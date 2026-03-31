#!/usr/bin/env python3.12
"""
Fast Tee Time Booking for Lochmere Golf Club (EZLinks)

Hybrid approach:
- nodriver for fast browser automation (no AI overhead)
- Direct DOM scraping for tee time data
- AI fallback only if something unexpected happens

Target: Complete booking in <45 seconds (mostly Cloudflare wait).
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from browser_use import Agent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession
from browser_use.llm.anthropic.chat import ChatAnthropic

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env")

EZLINKS_USERNAME = os.environ["EZLINKS_USERNAME"]
EZLINKS_PASSWORD = os.environ["EZLINKS_PASSWORD"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

TARGET_TIME = os.getenv("TARGET_TIME", "08:28")
NUM_PLAYERS = int(os.getenv("NUM_PLAYERS", "4"))
BOOKING_URL = os.getenv("BOOKING_URL", "https://lochmeregm.ezlinksgolf.com")
DAYS_OUT = int(os.getenv("DAYS_OUT", "14"))
BOOKING_HOUR = int(os.getenv("BOOKING_HOUR", "7"))  # Hour when new times drop
BOOKING_MINUTE = int(os.getenv("BOOKING_MINUTE", "30"))  # Minute when new times drop
POLL_LEAD_SECS = int(os.getenv("POLL_LEAD_SECS", "15"))  # Start polling this many seconds before drop

DRY_RUN = "--dry-run" in sys.argv

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(__file__).parent / "booking.log"),
    ],
)
log = logging.getLogger(__name__)


def time_to_minutes(time_str: str) -> int:
    """Convert time string like '08:28' or '8:28 AM' to minutes since midnight."""
    time_str = time_str.strip().upper()
    # Handle "8:28 AM" / "2:30 PM" format
    if "AM" in time_str or "PM" in time_str:
        is_pm = "PM" in time_str
        time_str = time_str.replace("AM", "").replace("PM", "").strip()
        parts = time_str.split(":")
        h, m = int(parts[0]), int(parts[1])
        if is_pm and h != 12:
            h += 12
        if not is_pm and h == 12:
            h = 0
        return h * 60 + m
    # Handle "08:28" / "14:30" format
    parts = time_str.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def minutes_to_time(mins: int) -> str:
    """Convert minutes since midnight to readable time."""
    h = mins // 60
    m = mins % 60
    period = "AM" if h < 12 else "PM"
    if h == 0:
        h = 12
    elif h > 12:
        h -= 12
    return f"{h}:{m:02d} {period}"



async def find_best_tee_time(tee_times, target_time, num_players):
    """Find the tee time closest to target with enough player slots."""
    target_mins = time_to_minutes(target_time)

    eligible = []
    for tt in tee_times:
        max_p = tt.get("maxPlayers", 0)
        if max_p >= num_players:
            tt_mins = time_to_minutes(tt["time"])
            diff = abs(tt_mins - target_mins)
            # Prefer slightly before over slightly after
            if tt_mins <= target_mins:
                diff -= 0.5  # Small bonus for being before target
            eligible.append((diff, tt))

    if not eligible:
        return None

    eligible.sort(key=lambda x: x[0])
    best = eligible[0][1]
    return best



async def main():
    target_date = datetime.now() + timedelta(days=DAYS_OUT)
    target_date_str = target_date.strftime("%m/%d/%Y")
    target_day_str = target_date.strftime("%A, %B %d, %Y")

    log.info("=" * 60)
    log.info("Tee Time Booking Bot (FAST MODE)")
    log.info("=" * 60)
    log.info(f"Target date:    {target_day_str} ({target_date_str})")
    log.info(f"Target time:    {TARGET_TIME}")
    log.info(f"Players:        {NUM_PLAYERS}")
    log.info(f"Booking URL:    {BOOKING_URL}")
    log.info(f"Dry run:        {DRY_RUN}")
    log.info("=" * 60)

    start_time = asyncio.get_event_loop().time()

    # ---------------------------------------------------------------
    # Phase 1: Use browser-use AI ONLY to pass Cloudflare
    # ---------------------------------------------------------------
    log.info("Phase 1: Passing Cloudflare with AI agent...")

    llm = ChatAnthropic(
        model="claude-sonnet-4-20250514",
        api_key=ANTHROPIC_API_KEY,
    )

    browser_profile = BrowserProfile(
        headless=False,
        disable_security=False,
        allowed_domains=[
            "lochmeregm.ezlinksgolf.com",
            "ezlinksgolf.com",
            "challenges.cloudflare.com",
        ],
    )
    browser_session = BrowserSession(browser_profile=browser_profile, keep_alive=True)

    cloudflare_task = f"""
    Go to {BOOKING_URL}

    If you see a Cloudflare "Verify you are human" checkbox, click it and wait
    for the page to load.

    Once the tee time booking page loads (you see tee times, a date picker,
    and player selection), your job is DONE. Report "CLOUDFLARE_PASSED" and stop.

    Do NOT interact with any booking elements. Just pass Cloudflare and stop.
    """

    agent = Agent(
        task=cloudflare_task,
        llm=llm,
        use_vision=True,
        browser_session=browser_session,
        max_failures=3,
        max_actions_per_step=2,
    )

    result = await agent.run()

    elapsed = asyncio.get_event_loop().time() - start_time
    log.info(f"[{elapsed:.1f}s] Cloudflare phase complete")

    # ---------------------------------------------------------------
    # Phase 2: Take over with direct CDP calls for speed
    # ---------------------------------------------------------------
    log.info("Phase 2: Fast automation with direct CDP...")

    page = await browser_session.get_current_page()
    if not page:
        log.error("Could not get page from browser session")
        sys.exit(1)

    js = page.evaluate  # shorthand

    debug_dir = Path(__file__).parent
    async def screenshot(name):
        try:
            import base64
            b64 = await page.screenshot(format='png')
            with open(str(debug_dir / f"debug_{name}.png"), 'wb') as f:
                f.write(base64.b64decode(b64))
            log.info(f"Screenshot saved: debug_{name}.png")
        except Exception as e:
            log.warning(f"Screenshot failed ({name}): {e}")

    try:
        # Step 1: Sign in
        log.info("Signing in...")
        await screenshot("01_before_signin")

        await js("""() => {
            var signIn = Array.from(document.querySelectorAll('a, button, span'))
                .find(el => el.textContent.trim() === 'Sign In');
            if (signIn) signIn.click();
        }""")
        await asyncio.sleep(2)
        await screenshot("02_signin_clicked")

        # Check what input fields are visible
        fields_info = await js("""() => {
            var inputs = document.querySelectorAll('input');
            var result = [];
            inputs.forEach(function(inp) {
                result.push({
                    type: inp.type,
                    name: inp.name,
                    id: inp.id,
                    placeholder: inp.placeholder,
                    visible: inp.offsetParent !== null
                });
            });
            return JSON.stringify(result);
        }""")
        log.info(f"Available input fields: {fields_info}")

        await js(f"""() => {{
            var userField = document.querySelector("input[name='username'], input[type='email'], input[id*='user'], input[id*='email'], input[name='email'], input[type='text']");
            if (userField) {{
                userField.focus();
                userField.value = '{EZLINKS_USERNAME}';
                userField.dispatchEvent(new Event('input', {{bubbles: true}}));
                userField.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}
        }}""")
        await asyncio.sleep(0.3)

        await js(f"""() => {{
            var passField = document.querySelector("input[type='password']");
            if (passField) {{
                passField.focus();
                passField.value = '{EZLINKS_PASSWORD}';
                passField.dispatchEvent(new Event('input', {{bubbles: true}}));
                passField.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}
        }}""")
        await asyncio.sleep(0.3)

        await js("""() => {
            var btn = document.querySelector("button[type='submit'], input[type='submit']");
            if (!btn) {
                btn = Array.from(document.querySelectorAll('button, a'))
                    .find(el => el.textContent.trim().match(/sign in|log in|submit/i));
            }
            if (btn) btn.click();
        }""")
        await asyncio.sleep(3)
        await screenshot("03_after_signin")

        # Verify login succeeded
        login_check = await js("""() => {
            var body = document.body.innerText;
            if (body.includes('Sign Out') || body.includes('Log Out') || body.includes('My Account') || body.includes('Welcome')) {
                return 'LOGGED_IN';
            }
            if (body.includes('Invalid') || body.includes('incorrect') || body.includes('failed')) {
                return 'LOGIN_FAILED';
            }
            return 'UNKNOWN: ' + body.substring(0, 300);
        }""")
        log.info(f"Login status: {login_check}")

        elapsed = asyncio.get_event_loop().time() - start_time
        log.info(f"[{elapsed:.1f}s] Signed in")

        # Step 2: Set date
        log.info(f"Setting date to {target_date_str}...")
        target_day = int(target_date_str.split("/")[1])

        await js(f"""() => {{
            var input = document.querySelector('input#pickerDate, input[datepicker]');
            if (input) {{
                input.value = '{target_date_str}';
                input.dispatchEvent(new Event('input', {{bubbles: true}}));
                input.dispatchEvent(new Event('change', {{bubbles: true}}));
                try {{ angular.element(input).triggerHandler('change'); }} catch(e) {{}}
            }}
        }}""")
        await asyncio.sleep(1)

        await js(f"""() => {{
            var cells = document.querySelectorAll('.ui-state-default');
            for (var cell of cells) {{
                if (cell.textContent.trim() === '{target_day}') {{
                    cell.click();
                    break;
                }}
            }}
        }}""")
        await asyncio.sleep(2)

        await screenshot("03b_after_date")
        elapsed = asyncio.get_event_loop().time() - start_time
        log.info(f"[{elapsed:.1f}s] Date set")

        # Step 3: Set players
        log.info(f"Setting players to {NUM_PLAYERS}...")
        # Wait for page to stabilize after date change
        await asyncio.sleep(2)
        await js(f"""() => {{
            var btn = document.querySelector('button#players-button');
            if (!btn) {{
                // Try finding by text content
                btn = Array.from(document.querySelectorAll('button'))
                    .find(b => b.id && b.id.includes('player'));
            }}
            if (btn) btn.click();
        }}""")
        await asyncio.sleep(1)
        await js(f"""() => {{
            var links = document.querySelectorAll('ul.dropdown-menu a, li a');
            for (var link of links) {{
                if (link.textContent.trim() === '{NUM_PLAYERS}') {{
                    link.click();
                    return true;
                }}
            }}
            return false;
        }}""")
        await asyncio.sleep(3)

        await screenshot("03c_after_players")
        elapsed = asyncio.get_event_loop().time() - start_time
        log.info(f"[{elapsed:.1f}s] Players set")

        # Step 3b: Select "Member Walk 18H" pricing option
        log.info("Selecting Member Walk 18H pricing option...")
        await js("""() => {
            var labels = document.querySelectorAll('label, span, a, div, button');
            for (var el of labels) {
                var text = el.textContent.trim();
                if (text === 'Member Walk 18H') {
                    el.click();
                    return true;
                }
            }
            // Try checkboxes/radio buttons near the text
            var inputs = document.querySelectorAll('input[type="checkbox"], input[type="radio"]');
            for (var inp of inputs) {
                var parent = inp.parentElement;
                if (parent && parent.textContent.includes('Member Walk 18H')) {
                    inp.click();
                    return true;
                }
            }
            return false;
        }""")
        await asyncio.sleep(3)
        await screenshot("03d_after_pricing")
        elapsed = asyncio.get_event_loop().time() - start_time
        log.info(f"[{elapsed:.1f}s] Pricing option set")

        # Wait for tee times to reload after player change
        log.info("Waiting for tee times to load...")
        await asyncio.sleep(3)

        # Scroll to top of tee times list first
        await js("""() => {
            window.scrollTo(0, 0);
        }""")
        await asyncio.sleep(1)

        # ---------------------------------------------------------------
        # WAIT FOR BOOKING WINDOW: Sit on page, then poll for new times
        # ---------------------------------------------------------------
        now = datetime.now()
        drop_time = now.replace(hour=BOOKING_HOUR, minute=BOOKING_MINUTE, second=0, microsecond=0)
        poll_start = drop_time - timedelta(seconds=POLL_LEAD_SECS)

        if now < poll_start:
            wait_secs = (poll_start - now).total_seconds()
            log.info(f"Pre-positioned! Waiting {wait_secs:.0f}s until {poll_start.strftime('%H:%M:%S')} to start polling...")
            log.info(f"Drop time: {drop_time.strftime('%H:%M:%S')}, polling starts {POLL_LEAD_SECS}s early")
            while True:
                now = datetime.now()
                remaining = (poll_start - now).total_seconds()
                if remaining <= 0:
                    break
                if remaining <= 10:
                    log.info(f"  {remaining:.1f}s until polling starts...")
                elif remaining <= 60:
                    if int(remaining) % 10 == 0:
                        log.info(f"  {remaining:.0f}s until polling starts...")
                else:
                    if int(remaining) % 30 == 0:
                        log.info(f"  {remaining:.0f}s until polling starts...")
                await asyncio.sleep(min(1.0, remaining))

            log.info("Starting rapid refresh polling for new tee times!")

        if now < drop_time:
            # Poll by refreshing the page rapidly until morning times appear
            target_minutes = time_to_minutes(TARGET_TIME)
            poll_attempt = 0
            MAX_POLL_ATTEMPTS = 60  # ~60 seconds of polling at 1s intervals
            tee_times = None

            while poll_attempt < MAX_POLL_ATTEMPTS:
                poll_attempt += 1
                now = datetime.now()
                log.info(f"Poll attempt {poll_attempt} at {now.strftime('%H:%M:%S.%f')[:-3]}...")

                # Refresh and re-apply filters
                await js("() => { location.reload(); }")
                await asyncio.sleep(3)

                # Re-set date
                await js(f"""() => {{
                    var input = document.querySelector('input#pickerDate, input[datepicker]');
                    if (input) {{
                        input.value = '{target_date_str}';
                        input.dispatchEvent(new Event('input', {{bubbles: true}}));
                        input.dispatchEvent(new Event('change', {{bubbles: true}}));
                        try {{ angular.element(input).triggerHandler('change'); }} catch(e) {{}}
                    }}
                }}""")
                await asyncio.sleep(1)
                await js(f"""() => {{
                    var cells = document.querySelectorAll('.ui-state-default');
                    for (var cell of cells) {{
                        if (cell.textContent.trim() === '{target_day}') {{
                            cell.click();
                            break;
                        }}
                    }}
                }}""")
                await asyncio.sleep(1)

                # Re-set players
                await js(f"""() => {{
                    var btn = document.querySelector('button#players-button');
                    if (!btn) {{
                        btn = Array.from(document.querySelectorAll('button'))
                            .find(b => b.id && b.id.includes('player'));
                    }}
                    if (btn) btn.click();
                }}""")
                await asyncio.sleep(0.5)
                await js(f"""() => {{
                    var links = document.querySelectorAll('ul.dropdown-menu a, li a');
                    for (var link of links) {{
                        if (link.textContent.trim() === '{NUM_PLAYERS}') {{
                            link.click();
                            return true;
                        }}
                    }}
                    return false;
                }}""")
                await asyncio.sleep(1)

                # Re-select pricing
                await js("""() => {
                    var labels = document.querySelectorAll('label, span, a, div, button');
                    for (var el of labels) {
                        var text = el.textContent.trim();
                        if (text === 'Member Walk 18H') {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }""")
                await asyncio.sleep(1)

                # Quick scrape — check if morning times exist
                quick_check = await js("""() => {
                    var body = document.body.innerText;
                    // Look for morning times (before noon)
                    var morningTimes = body.match(/\\b[5-9]:\\d{2}\\s*AM|\\b1[0-1]:\\d{2}\\s*AM/gi);
                    return morningTimes ? morningTimes.join(',') : '';
                }""")

                if quick_check:
                    log.info(f"Morning times detected: {quick_check}")
                    await js("() => { window.scrollTo(0, 0); }")
                    await asyncio.sleep(1)
                    break
                else:
                    log.info("  No morning times yet...")

            if not quick_check:
                log.error(f"No morning times appeared after {MAX_POLL_ATTEMPTS} poll attempts")
                await screenshot("04_poll_exhausted")
                sys.exit(1)

            log.info("Morning times are live! Proceeding to scrape and book...")
        else:
            log.info("Booking window already open, proceeding immediately")

        # Step 4: Scrape tee times
        await screenshot("04_before_scrape")

        # First dump page structure for debugging
        page_info = await js("""() => {
            var info = {};
            info.url = window.location.href;
            var viewBtns = Array.from(document.querySelectorAll('a, button'))
                .filter(function(el) { return el.textContent.trim() === 'VIEW'; });
            info.viewButtonCount = viewBtns.length;
            if (viewBtns.length > 0) {
                var container = viewBtns[0].parentElement;
                var chain = [];
                for (var i = 0; i < 6 && container; i++) {
                    chain.push(container.tagName + '.' + container.className);
                    container = container.parentElement;
                }
                info.viewParentChain = chain;
                var card = viewBtns[0].closest('.panel, .card, .col-md-4, .col-sm-4, [ng-repeat]');
                info.firstCardText = card ? card.innerText.substring(0, 200) : 'no card found';
            }
            info.bodyPreview = document.body.innerText.substring(0, 500);
            return JSON.stringify(info);
        }""")
        log.info(f"Page structure: {page_info}")

        SCRAPE_JS = """() => {
            var results = [];

            // Strategy 0: Try to get data directly from Angular scope (fastest, gets ALL times)
            try {
                var scope = angular.element(document.querySelector('[ng-repeat]')).scope();
                if (scope && scope.$parent) {
                    var parentScope = scope.$parent;
                    // Look for tee time array in scope
                    var keys = Object.keys(parentScope);
                    for (var k = 0; k < keys.length; k++) {
                        var val = parentScope[keys[k]];
                        if (Array.isArray(val) && val.length > 0 && val[0] && (val[0].time || val[0].teeTime || val[0].startTime)) {
                            val.forEach(function(tt) {
                                var timeStr = tt.time || tt.teeTime || tt.startTime || '';
                                if (timeStr) {
                                    results.push({
                                        time: timeStr,
                                        maxPlayers: tt.maxPlayers || tt.max_players || 4,
                                        minPlayers: tt.minPlayers || tt.min_players || 1,
                                        price: tt.price || tt.greenFee || 'N/A',
                                    });
                                }
                            });
                            if (results.length > 0) {
                                var seen = {};
                                return JSON.stringify(results.filter(function(r) {
                                    if (seen[r.time]) return false;
                                    seen[r.time] = true;
                                    return true;
                                }));
                            }
                        }
                    }
                }
            } catch(e) {}

            // Strategy 1: Find VIEW buttons and walk up to their tee time cards
            var viewBtns = Array.from(document.querySelectorAll('a, button'))
                .filter(function(el) { return el.textContent.trim() === 'VIEW'; });

            viewBtns.forEach(function(btn) {
                var container = btn.closest('.panel, .card, .col-md-4, .col-sm-4, .col-lg-4, [ng-repeat], [data-ng-repeat]');
                if (!container) {
                    container = btn.parentElement;
                    for (var i = 0; i < 5 && container; i++) {
                        if (container.innerText && container.innerText.match(/\\d{1,2}:\\d{2}\\s*[AP]M/i)) break;
                        container = container.parentElement;
                    }
                }
                if (!container) return;

                var text = container.innerText || '';
                var timeMatch = text.match(/(\\d{1,2}:\\d{2}\\s*[AP]M)/i);
                var playerMatch = text.match(/(\\d+)[\\u2013-](\\d+)\\s*Player/i);
                var priceMatch = text.match(/\\$(\\d+\\.\\d+)/);

                if (timeMatch) {
                    var parts = timeMatch[1].match(/(\\d+):(\\d+)/);
                    var h = parseInt(parts[1]);
                    var m = parseInt(parts[2]);
                    if (h >= 1 && h <= 12 && m >= 0 && m <= 59) {
                        results.push({
                            time: timeMatch[1],
                            maxPlayers: playerMatch ? parseInt(playerMatch[2]) : 4,
                            minPlayers: playerMatch ? parseInt(playerMatch[1]) : 1,
                            price: priceMatch ? priceMatch[0] : 'N/A',
                        });
                    }
                }
            });

            // Strategy 2: ng-repeat elements
            if (results.length === 0) {
                var ngRepeats = document.querySelectorAll('[ng-repeat], [data-ng-repeat]');
                ngRepeats.forEach(function(el) {
                    var text = el.innerText || '';
                    var timeMatch = text.match(/(\\d{1,2}:\\d{2}\\s*[AP]M)/i);
                    if (timeMatch) {
                        var playerMatch = text.match(/(\\d+)[\\u2013-](\\d+)\\s*Player/i);
                        var priceMatch = text.match(/\\$(\\d+\\.\\d+)/);
                        var parts = timeMatch[1].match(/(\\d+):(\\d+)/);
                        var h = parseInt(parts[1]);
                        var m = parseInt(parts[2]);
                        if (h >= 1 && h <= 12 && m >= 0 && m <= 59) {
                            results.push({
                                time: timeMatch[1],
                                maxPlayers: playerMatch ? parseInt(playerMatch[2]) : 4,
                                minPlayers: playerMatch ? parseInt(playerMatch[1]) : 1,
                                price: priceMatch ? priceMatch[0] : 'N/A',
                            });
                        }
                    }
                });
            }

            // Strategy 3: Parse body text but only lines that look like standalone times
            if (results.length === 0) {
                var body = document.body.innerText;
                var lines = body.split('\\n');
                for (var i = 0; i < lines.length; i++) {
                    var line = lines[i].trim();
                    var timeMatch = line.match(/^(\\d{1,2}:\\d{2}\\s*[AP]M)$/i);
                    if (timeMatch) {
                        var nearby = lines.slice(Math.max(0, i-3), Math.min(lines.length, i+5)).join(' ');
                        if (nearby.includes('VIEW') || nearby.match(/Player/i)) {
                            var parts = timeMatch[1].match(/(\\d+):(\\d+)/);
                            var h = parseInt(parts[1]);
                            var m = parseInt(parts[2]);
                            if (h >= 1 && h <= 12 && m >= 0 && m <= 59) {
                                var playerMatch = nearby.match(/(\\d+)[\\u2013-](\\d+)\\s*Player/i);
                                var priceMatch = nearby.match(/\\$(\\d+\\.\\d+)/);
                                results.push({
                                    time: timeMatch[1],
                                    maxPlayers: playerMatch ? parseInt(playerMatch[2]) : 4,
                                    minPlayers: playerMatch ? parseInt(playerMatch[1]) : 1,
                                    price: priceMatch ? priceMatch[0] : 'N/A',
                                });
                            }
                        }
                    }
                }
            }

            var seen = {};
            return JSON.stringify(results.filter(function(r) {
                if (seen[r.time]) return false;
                seen[r.time] = true;
                return true;
            }));
        }"""

        log.info("Scraping tee times (scrolling to collect all)...")

        # Scroll through the page to load all tee times, collecting as we go
        all_times = set()
        all_results = []
        for scroll_pass in range(10):
            raw = await js(SCRAPE_JS)
            if isinstance(raw, str):
                import json as _json
                batch = _json.loads(raw)
            elif isinstance(raw, list):
                batch = raw
            else:
                batch = []
            new_count = 0
            for tt in batch:
                if isinstance(tt, dict) and tt.get('time') and tt['time'] not in all_times:
                    all_times.add(tt['time'])
                    all_results.append(tt)
                    new_count += 1
            log.info(f"  Scroll pass {scroll_pass+1}: found {len(batch)} times, {new_count} new (total: {len(all_results)})")
            if scroll_pass < 9:
                await js("() => { window.scrollBy(0, 800); }")
                await asyncio.sleep(0.5)

        # Use collected results instead of single scrape
        raw_result = json.dumps(all_results)
        log.info(f"Total unique tee times collected: {len(all_results)}")
        log.info(f"Raw scrape result type: {type(raw_result)}, preview: {str(raw_result)[:200]}")

        # CDP evaluate may return the data in different formats
        if isinstance(raw_result, str):
            import json as _json
            tee_times = _json.loads(raw_result)
        elif isinstance(raw_result, list):
            tee_times = raw_result
        else:
            tee_times = raw_result

        if not tee_times:
            log.warning("No tee times found, retrying in 3s...")
            await asyncio.sleep(3)
            raw_result = await js(SCRAPE_JS)
            if isinstance(raw_result, str):
                tee_times = _json.loads(raw_result)
            elif isinstance(raw_result, list):
                tee_times = raw_result
            else:
                tee_times = raw_result

        if not tee_times:
            log.error("No tee times found")
            await page.screenshot(str(Path(__file__).parent / "debug_no_times.png"))
            sys.exit(1)

        # Ensure tee_times is a list of dicts
        if isinstance(tee_times, list) and tee_times and isinstance(tee_times[0], str):
            log.warning(f"Tee times returned as strings, attempting to parse")
            tee_times = [_json.loads(t) if isinstance(t, str) else t for t in tee_times]

        elapsed = asyncio.get_event_loop().time() - start_time
        log.info(f"[{elapsed:.1f}s] Found {len(tee_times)} tee times")

        for tt in tee_times:
            if isinstance(tt, dict):
                log.info(f"  {tt['time']} - {tt.get('price', 'N/A')} - {tt.get('minPlayers', '?')}-{tt.get('maxPlayers', '?')} Players")
            else:
                log.info(f"  Raw: {tt}")

        # Step 5: Find best time (closest to target, no restrictions)
        best = await find_best_tee_time(tee_times, TARGET_TIME, NUM_PLAYERS)

        if not best:
            log.error(f"No tee times with {NUM_PLAYERS} player slots")
            sys.exit(1)

        target_mins = time_to_minutes(TARGET_TIME)
        best_mins = time_to_minutes(best["time"])
        diff = abs(best_mins - target_mins)

        log.info("=" * 60)
        log.info(f"BEST TEE TIME: {best['time']}")
        log.info(f"  Price: {best.get('price', 'N/A')}")
        log.info(f"  Players: {best.get('minPlayers', '?')}-{best.get('maxPlayers', '?')}")
        log.info(f"  Difference from target: {diff} minutes")
        log.info("=" * 60)

        if DRY_RUN:
            elapsed = asyncio.get_event_loop().time() - start_time
            log.info(f"DRY RUN complete in {elapsed:.1f}s - would book {best['time']}")
            await asyncio.sleep(5)
            return

        # Step 6: Scroll back to top and click VIEW for the best time
        await js("() => { window.scrollTo(0, 0); }")
        await asyncio.sleep(1)

        log.info(f"Clicking VIEW for {best['time']}...")

        # First, let's understand what VIEW elements look like
        view_debug = await js("""() => {
            var allEls = document.querySelectorAll('*');
            var viewEls = [];
            for (var el of allEls) {
                if (el.childNodes.length <= 2 && el.textContent.trim() === 'VIEW') {
                    viewEls.push({
                        tag: el.tagName,
                        className: el.className,
                        parentTag: el.parentElement ? el.parentElement.tagName : 'none',
                        parentClass: el.parentElement ? el.parentElement.className : 'none',
                    });
                }
            }
            return JSON.stringify({count: viewEls.length, samples: viewEls.slice(0, 3)});
        }""")
        log.info(f"VIEW element structure: {view_debug}")

        clicked = await js(f"""() => {{
            // Find ALL elements with text "VIEW" (any tag)
            var viewEls = [];
            var allEls = document.querySelectorAll('a, button, span, div, label');
            for (var el of allEls) {{
                if (el.textContent.trim() === 'VIEW' || el.textContent.trim() === 'View') {{
                    viewEls.push(el);
                }}
            }}

            // For each VIEW element, check if a parent contains the target time
            for (var btn of viewEls) {{
                var parent = btn;
                for (var i = 0; i < 8 && parent; i++) {{
                    if (parent.innerText && parent.innerText.includes('{best["time"]}')) {{
                        btn.click();
                        return 'found_' + btn.tagName;
                    }}
                    parent = parent.parentElement;
                }}
            }}

            // Fallback: just click the first VIEW element (it should be the closest to target after scroll to top)
            if (viewEls.length > 0) {{
                viewEls[0].click();
                return 'fallback_first';
            }}

            return null;
        }}""")

        if not clicked:
            log.error(f"Could not click VIEW for {best['time']}")
            await screenshot("06_view_click_failed")
            sys.exit(1)

        log.info(f"VIEW clicked via '{clicked}' strategy")
        await asyncio.sleep(5)
        await screenshot("06_after_view_click")

        # Dismiss any "Existing Reservation" or "Attention" dialogs
        dismissed = await js("""() => {
            var dismissed = 0;
            var buttons = document.querySelectorAll('button');
            for (var btn of buttons) {
                if (btn.textContent.trim() === 'OK' && btn.offsetParent !== null) {
                    btn.click();
                    dismissed++;
                }
            }
            return dismissed;
        }""")
        if dismissed:
            log.info(f"Dismissed {dismissed} attention dialog(s)")
            await asyncio.sleep(2)

        # Dump page content to understand the booking page
        booking_page = await js("() => document.body.innerText.substring(0, 2000)")
        log.info(f"Booking page content:\n{booking_page}")

        # Handle "Tee Time Adjustment" dialog — time was sniped by someone else
        # Retry up to 5 times with next-best available time
        tried_times = set()
        MAX_RETRIES = 5
        for attempt in range(MAX_RETRIES):
            if "Tee Time Adjustment" not in booking_page and "no longer available" not in booking_page:
                break  # No adjustment dialog — proceed with booking

            tried_times.add(best['time'])
            log.warning(f"Attempt {attempt + 1}/{MAX_RETRIES}: {best['time']} was taken! Got 'Tee Time Adjustment' dialog.")

            # Click "No" to decline the alternative
            declined = await js("""() => {
                var buttons = document.querySelectorAll('button, a, input[type="button"]');
                for (var btn of buttons) {
                    if (btn.textContent.trim() === 'No' && btn.offsetParent !== null) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
            log.info(f"Declined alternative: {declined}")
            await asyncio.sleep(2)

            # Pick the next best time we haven't tried yet
            available_times = [t for t in tee_times if t['time'] not in tried_times]
            if not available_times:
                log.error("All tee times have been taken!")
                await screenshot(f"06_no_alternatives_attempt{attempt + 1}")
                sys.exit(1)

            best = min(available_times, key=lambda t: abs(time_to_minutes(t['time']) - target_minutes))
            log.info(f"Trying next best time: {best['time']}")

            # Click VIEW for the new time
            retry_clicked = await js(f"""() => {{
                var allEls = Array.from(document.querySelectorAll('*'));
                var viewEls = allEls.filter(function(el) {{
                    return el.textContent.trim() === 'VIEW' || el.textContent.trim() === 'View';
                }});
                for (var vel of viewEls) {{
                    var parent = vel;
                    for (var i = 0; i < 8; i++) {{
                        if (!parent) break;
                        if (parent.textContent.includes('{best["time"]}')) {{
                            vel.click();
                            return 'found_for_' + '{best["time"]}';
                        }}
                        parent = parent.parentElement;
                    }}
                }}
                if (viewEls.length > 0) {{
                    viewEls[0].click();
                    return 'found_BUTTON';
                }}
                return null;
            }}""")

            if not retry_clicked:
                log.error(f"Could not click VIEW for {best['time']}")
                await screenshot(f"06_retry{attempt + 1}_view_failed")
                sys.exit(1)

            log.info(f"VIEW clicked via '{retry_clicked}'")
            await asyncio.sleep(5)
            await screenshot(f"06_retry{attempt + 1}_after_view")

            booking_page = await js("() => document.body.innerText.substring(0, 2000)")
            log.info(f"Retry {attempt + 1} page content:\n{booking_page}")
        else:
            # Exhausted all retries
            log.error(f"All {MAX_RETRIES} attempts failed — every time was sniped!")
            await screenshot("06_all_retries_exhausted")
            sys.exit(1)

        # Step 7: Select "Member Walk 18H" from the options modal
        log.info("Selecting Member Walk 18H from options modal...")

        # First, dump the modal DOM structure for debugging
        modal_debug = await js("""() => {
            // Find the modal
            var modal = null;
            var candidates = document.querySelectorAll('.modal, .modal-dialog, .modal-content, [class*="modal"], [class*="popup"], [class*="dialog"], [class*="overlay"]');
            for (var el of candidates) {
                if (el.innerText && el.innerText.includes('CHOOSE OPTION')) {
                    modal = el;
                    break;
                }
            }
            if (!modal) {
                var headers = document.querySelectorAll('*');
                for (var h of headers) {
                    if (h.textContent.trim().startsWith('CHOOSE OPTION')) {
                        modal = h.parentElement;
                        break;
                    }
                }
            }
            if (!modal) return JSON.stringify({error: 'no modal found'});

            // Dump all clickable-looking elements in the modal
            var info = {modalTag: modal.tagName, modalClass: modal.className};
            var children = modal.querySelectorAll('*');
            var rows = [];
            children.forEach(function(el) {
                var text = el.textContent.trim();
                if (text.includes('Walk 18H') && !text.includes('Ride') && !text.includes('CHOOSE')) {
                    rows.push({
                        tag: el.tagName,
                        className: el.className,
                        text: text.substring(0, 80),
                        childCount: el.childNodes.length,
                        hasAnchor: el.querySelectorAll('a').length,
                        hasButton: el.querySelectorAll('button').length,
                        hasInput: el.querySelectorAll('input').length,
                        isClickable: el.tagName === 'A' || el.tagName === 'BUTTON' || el.style.cursor === 'pointer' || el.onclick !== null,
                        ngClick: el.getAttribute('ng-click') || el.getAttribute('data-ng-click') || '',
                    });
                }
            });
            info.walkRows = rows;

            // Also get ALL direct children of the modal body to understand structure
            var directRows = [];
            // Look for the options container (usually a list/table)
            var containers = modal.querySelectorAll('table, tbody, ul, ol, .modal-body, [class*="body"], [class*="options"], [class*="list"]');
            containers.forEach(function(c) {
                var cChildren = c.children;
                for (var i = 0; i < cChildren.length && i < 10; i++) {
                    var ch = cChildren[i];
                    directRows.push({
                        tag: ch.tagName,
                        className: ch.className,
                        text: ch.textContent.trim().substring(0, 60),
                        ngClick: ch.getAttribute('ng-click') || ch.getAttribute('data-ng-click') || '',
                    });
                }
            });
            info.containerRows = directRows;

            return JSON.stringify(info);
        }""")
        log.info(f"Modal structure: {modal_debug}")

        # Now click the correct element
        option_result = await js("""() => {
            // Find the modal
            var modal = null;
            var candidates = document.querySelectorAll('.modal, .modal-dialog, .modal-content, [class*="modal"], [class*="popup"], [class*="dialog"], [class*="overlay"]');
            for (var el of candidates) {
                if (el.innerText && el.innerText.includes('CHOOSE OPTION')) {
                    modal = el;
                    break;
                }
            }
            if (!modal) {
                var headers = document.querySelectorAll('*');
                for (var h of headers) {
                    if (h.textContent.trim().startsWith('CHOOSE OPTION')) {
                        modal = h.parentElement;
                        break;
                    }
                }
            }
            if (!modal) return 'no_modal_found';

            // Strategy 1: Find element with ng-click containing the Walk option
            var ngClickEls = modal.querySelectorAll('[ng-click], [data-ng-click]');
            for (var el of ngClickEls) {
                var text = el.textContent.trim();
                if (text.includes('Member Walk 18H') && !text.includes('Ride') && !text.includes('CHOOSE')) {
                    el.click();
                    return 'ng-click: ' + (el.getAttribute('ng-click') || el.getAttribute('data-ng-click'));
                }
            }

            // Strategy 2: Find <a> or <button> elements inside a Walk 18H row
            var allEls = modal.querySelectorAll('*');
            for (var el of allEls) {
                var text = el.textContent.trim();
                if (text.includes('Member Walk 18H') && !text.includes('Ride') && !text.includes('CHOOSE') && !text.includes('9H')) {
                    // Look for clickable children first
                    var clickable = el.querySelector('a, button, input[type="radio"], input[type="checkbox"]');
                    if (clickable) {
                        clickable.click();
                        return 'child_clickable: ' + clickable.tagName;
                    }
                    // If this element itself is a table row or list item, click it
                    if (el.tagName === 'TR' || el.tagName === 'LI' || el.tagName === 'A' || el.tagName === 'BUTTON') {
                        el.click();
                        return 'direct_click: ' + el.tagName;
                    }
                }
            }

            // Strategy 3: Find the exact leaf text node "Member Walk 18H" and click its parent
            for (var el of allEls) {
                if (el.childNodes.length <= 2 && el.textContent.trim() === 'Member Walk 18H') {
                    // Click the nearest row-like ancestor
                    var row = el.closest('tr, li, [ng-click], [data-ng-click], a, button');
                    if (row) {
                        row.click();
                        return 'leaf_parent: ' + row.tagName;
                    }
                    // Click parent
                    el.parentElement.click();
                    return 'leaf_direct_parent: ' + el.parentElement.tagName;
                }
            }

            return 'walk_18h_not_clickable';
        }""")
        log.info(f"Option select result: {option_result}")
        await asyncio.sleep(2)
        await screenshot("07_after_option_select")

        # Check if modal is still showing or if we navigated
        page_after_option = await js("() => document.body.innerText.substring(0, 500)")
        log.info(f"After option select:\n{page_after_option}")

        # Set # of Players in the modal to match our target
        log.info(f"Setting modal player count to {NUM_PLAYERS}...")
        modal_players = await js(f"""() => {{
            // Find the "# of Players" dropdown in the modal
            var selects = document.querySelectorAll('select');
            for (var sel of selects) {{
                // Check if this select is for players (look at label or options)
                var label = sel.closest('.form-group, .row, div')?.querySelector('label');
                var isPlayerSelect = (label && label.textContent.includes('Player')) ||
                    sel.id.toLowerCase().includes('player') ||
                    sel.name?.toLowerCase().includes('player');

                // Also check by option values — player selects have 1,2,3,4
                if (!isPlayerSelect) {{
                    var opts = Array.from(sel.options).map(o => o.text.trim());
                    if (opts.includes('1 player') || opts.includes('2 players') ||
                        (opts.includes('1') && opts.includes('2') && opts.includes('3') && opts.includes('4'))) {{
                        isPlayerSelect = true;
                    }}
                }}

                if (isPlayerSelect) {{
                    // Find the option matching our player count
                    for (var opt of sel.options) {{
                        if (opt.text.trim() === '{NUM_PLAYERS} players' || opt.text.trim() === '{NUM_PLAYERS}') {{
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                            try {{ angular.element(sel).triggerHandler('change'); }} catch(e) {{}}
                            return 'set_to_{NUM_PLAYERS}';
                        }}
                    }}
                }}
            }}
            return 'no_player_select_found';
        }}""")
        log.info(f"Modal player select result: {modal_players}")
        await asyncio.sleep(1)

        # If still on modal (Continue button needed), click it
        log.info("Looking for Continue button...")
        continued = await js("""() => {
            // First try visible Continue buttons
            var buttons = document.querySelectorAll('button, a, input[type="submit"]');
            for (var btn of buttons) {
                var text = btn.textContent.trim();
                if (text === 'Continue' && btn.offsetParent !== null) {
                    btn.click();
                    return 'visible';
                }
            }
            // Try any Continue button
            for (var btn of buttons) {
                var text = btn.textContent.trim();
                if (text === 'Continue') {
                    btn.click();
                    return 'any';
                }
            }
            return false;
        }""")
        log.info(f"Continue button result: {continued}")
        await asyncio.sleep(5)
        await screenshot("07b_after_continue")

        # Check what page we're on now - should be "Verify Details"
        page_content = await js("() => document.body.innerText.substring(0, 1500)")
        log.info(f"After continue:\n{page_content}")

        # Step 8: On Verify Details page, click CONTINUE to proceed to final confirmation
        log.info("Looking for CONTINUE on Verify Details page...")
        verify_continue = await js("""() => {
            var buttons = document.querySelectorAll('button, a, input[type="submit"]');
            for (var btn of buttons) {
                var text = btn.textContent.trim();
                if ((text === 'Continue' || text === 'CONTINUE') && btn.offsetParent !== null) {
                    btn.click();
                    return text;
                }
            }
            return null;
        }""")
        log.info(f"Verify Details Continue result: {verify_continue}")
        await asyncio.sleep(5)
        await screenshot("08_after_verify_continue")

        # Dismiss any "Existing Reservation" dialogs on final page
        dismissed2 = await js("""() => {
            var dismissed = 0;
            var buttons = document.querySelectorAll('button');
            for (var btn of buttons) {
                if (btn.textContent.trim() === 'OK' && btn.offsetParent !== null) {
                    btn.click();
                    dismissed++;
                }
            }
            return dismissed;
        }""")
        if dismissed2:
            log.info(f"Dismissed {dismissed2} attention dialog(s) on final page")
            await asyncio.sleep(2)

        # Check final page
        final_content = await js("() => document.body.innerText.substring(0, 1500)")
        log.info(f"Final page content:\n{final_content}")

        # Step 9: Look for final Book/Confirm button
        log.info("Looking for final booking button...")
        all_buttons = await js("""() => {
            var buttons = document.querySelectorAll('a, button, input[type="submit"]');
            var result = [];
            buttons.forEach(function(btn) {
                var text = btn.textContent.trim();
                if (text && text.length < 50 && btn.offsetParent !== null) {
                    result.push({text: text, tag: btn.tagName, type: btn.type || ''});
                }
            });
            return JSON.stringify(result);
        }""")
        log.info(f"Visible buttons: {all_buttons}")

        booked = await js("""() => {
            var buttons = document.querySelectorAll('a, button, input[type="submit"]');
            var bookWords = ['book', 'reserve', 'confirm', 'complete', 'submit', 'finish'];
            for (var btn of buttons) {
                var text = btn.textContent.trim().toLowerCase();
                if (btn.offsetParent === null) continue;
                for (var word of bookWords) {
                    if (text.includes(word) && !text.includes('cancel')) {
                        btn.click();
                        return text;
                    }
                }
            }
            return null;
        }""")

        if booked:
            log.info(f"Clicked final booking button: '{booked}'")
            await asyncio.sleep(5)
            await screenshot("09_after_book_click")
        else:
            log.error("Could not find final booking button")
            await screenshot("09_no_book_button")
            sys.exit(1)

        await asyncio.sleep(5)
        elapsed = asyncio.get_event_loop().time() - start_time
        log.info("=" * 60)
        log.info(f"BOOKING FLOW COMPLETED in {elapsed:.1f}s!")
        log.info(f"Time: {best['time']} on {target_day_str}")
        log.info(f"Players: {NUM_PLAYERS}")
        log.info("=" * 60)

        confirmation = await js("() => document.body.innerText.substring(0, 2000)")
        log.info(f"Page content:\n{confirmation}")

    except Exception as e:
        log.exception(f"Error: {e}")
        await screenshot("error")
        sys.exit(1)
    finally:
        await asyncio.sleep(3)
        await browser_session.stop()


if __name__ == "__main__":
    asyncio.run(main())
