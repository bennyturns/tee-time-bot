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
DEBUG = "--debug" in sys.argv

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



async def wait_for(page, condition_js, timeout=5.0, interval=0.25, desc="condition"):
    """Poll a JS condition until it returns truthy, or timeout."""
    elapsed = 0.0
    while elapsed < timeout:
        result = await page.evaluate(condition_js)
        if result:
            return result
        await asyncio.sleep(interval)
        elapsed += interval
    log.warning(f"wait_for timed out after {timeout}s waiting for: {desc}")
    return None


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
    log.info(f"Debug:          {DEBUG}")
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

    MAX_CF_RETRIES = 3
    cf_passed = False

    for cf_attempt in range(1, MAX_CF_RETRIES + 1):
        log.info(f"Cloudflare agent attempt {cf_attempt}/{MAX_CF_RETRIES}...")

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
        log.info(f"[{elapsed:.1f}s] Agent attempt {cf_attempt} finished")

        # Verify Cloudflare is actually passed (agent sometimes reports false success)
        page_check = await browser_session.get_current_page()
        if not page_check:
            log.error("Could not get page for Cloudflare verification")
            sys.exit(1)

        for poll_s in range(0, 20, 2):
            cf_check = await page_check.evaluate("""() => {
                var body = document.body ? document.body.innerText : '';
                var hasChallenge = body.includes('Verify you are human') ||
                                  body.includes('Just a moment') ||
                                  body.includes('security verification');
                var hasBookingContent = body.includes('Sign In') ||
                                       body.includes('Tee Times') ||
                                       body.includes('Player');
                return JSON.stringify({hasChallenge: hasChallenge, hasBookingContent: hasBookingContent});
            }""")
            cf_status = json.loads(cf_check) if isinstance(cf_check, str) else cf_check
            if not cf_status.get("hasChallenge") and cf_status.get("hasBookingContent"):
                log.info("Cloudflare passed — booking page confirmed.")
                cf_passed = True
                break
            if cf_status.get("hasChallenge"):
                log.info(f"  Verify poll +{poll_s}s: challenge still active")
                break
            log.info(f"  Verify poll +{poll_s}s: page transitioning...")
            await asyncio.sleep(2)

        if cf_passed:
            break
        if cf_attempt >= MAX_CF_RETRIES:
            log.error(f"Cloudflare not passed after {MAX_CF_RETRIES} attempts. Aborting.")
            sys.exit(1)

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

    # Disable auto-attach — browser-use enables this at startup which causes Chrome to
    # create CDP sessions for every iframe/worker/popup. The EZLinks site detects these
    # extra sessions as "multiple tabs" and shows a logout warning.
    try:
        await browser_session._cdp_client_root.send.Target.setAutoAttach(
            params={'autoAttach': False, 'waitForDebuggerOnStart': False, 'flatten': True}
        )
        log.info("Disabled CDP auto-attach to prevent multi-tab detection")
    except Exception as e:
        log.warning(f"Could not disable auto-attach: {e}")

    # Use browser-use's existing page.evaluate — do NOT create additional CDP sessions
    js = page.evaluate

    # --- Diagnostic helpers (--debug mode) ---
    cdp_root = browser_session._cdp_client_root

    async def dump_targets(label):
        """Log all Chrome targets — shows exactly what Chrome thinks is open."""
        if not DEBUG:
            return
        try:
            result = await cdp_root.send.Target.getTargets(params={})
            targets = result.get('targetInfos', [])
            log.info(f"=== CDP TARGETS [{label}] === ({len(targets)} total)")
            for t in targets:
                log.info(f"  type={t.get('type', '?'):15s}  attached={str(t.get('attached', '?')):5s}  "
                         f"url={t.get('url', '?')[:80]}")
            log.info(f"=== END TARGETS [{label}] ===")
        except Exception as e:
            log.warning(f"dump_targets({label}) failed: {e}")

    async def dump_sessions(label):
        """Log all CDP sessions browser-use is tracking."""
        if not DEBUG:
            return
        try:
            sm = browser_session._session_manager
            sessions = getattr(sm, '_sessions', {})
            targets = getattr(sm, '_targets', {})
            log.info(f"=== CDP SESSIONS [{label}] === ({len(sessions)} sessions, {len(targets)} tracked targets)")
            for sid, sess in sessions.items():
                tid = getattr(sm, '_session_to_target', {}).get(sid, '?')
                tgt = targets.get(tid)
                tgt_url = getattr(tgt, 'url', '?') if tgt else '?'
                tgt_type = getattr(tgt, 'type', '?') if tgt else '?'
                log.info(f"  session={str(sid)[:20]}  target_type={tgt_type}  url={str(tgt_url)[:80]}")
            log.info(f"=== END SESSIONS [{label}] ===")
        except Exception as e:
            log.warning(f"dump_sessions({label}) failed: {e}")

    async def check_popup(label):
        """Check if the 'multiple tabs' popup is visible right now."""
        try:
            popup_text = await js("""() => {
                var body = document.body ? document.body.innerText.substring(0, 3000) : '';
                var checks = {
                    hasMultipleTabs: /multiple\s+tab/i.test(body),
                    hasLoggedOut: /being\s+logged\s+out/i.test(body),
                    hasSessionExpired: /session.*expir/i.test(body),
                    hasSignedOut: /signed\s+out/i.test(body),
                    currentUrl: window.location.href,
                    modalVisible: !!(document.querySelector('.modal.in, .modal.show, .modal[style*="display: block"]'))
                };
                return JSON.stringify(checks);
            }""")
            result = json.loads(popup_text) if isinstance(popup_text, str) else popup_text
            has_issue = result.get('hasMultipleTabs') or result.get('hasLoggedOut') or result.get('hasSessionExpired') or result.get('hasSignedOut')
            if has_issue:
                log.warning(f"!!! POPUP DETECTED [{label}] !!! {json.dumps(result, indent=2)}")
            elif DEBUG:
                log.info(f"Popup check [{label}]: clean — url={result.get('currentUrl', '?')[:60]} modal={result.get('modalVisible')}")
            return has_issue
        except Exception as e:
            log.warning(f"check_popup({label}) failed: {e}")
            return False

    debug_dir = Path(__file__).parent
    async def screenshot(name):
        if not DEBUG:
            return
        try:
            import base64
            b64 = await page.screenshot(format='png')
            with open(str(debug_dir / f"debug_{name}.png"), 'wb') as f:
                f.write(base64.b64decode(b64))
            log.info(f"Screenshot saved: debug_{name}.png")
        except Exception as e:
            log.warning(f"Screenshot failed ({name}): {e}")

    # Diagnostic: dump state right after Cloudflare
    await dump_targets("after_cloudflare")
    await dump_sessions("after_cloudflare")
    await check_popup("after_cloudflare")

    try:
        # Step 1: Sign in
        log.info("Signing in...")
        await screenshot("01_before_signin")
        await check_popup("before_signin_click")

        await js("""() => {
            var signIn = Array.from(document.querySelectorAll('a, button, span'))
                .find(el => el.textContent.trim() === 'Sign In');
            if (signIn) signIn.click();
        }""")
        await asyncio.sleep(2)
        await screenshot("02_signin_clicked")
        await check_popup("after_signin_modal_open")

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
        await dump_targets("after_submit_login")
        await dump_sessions("after_submit_login")
        await check_popup("after_submit_login")

        if DEBUG:
            # Check again after a brief wait — popup may appear with a delay
            await asyncio.sleep(2)
            await check_popup("after_submit_login_+2s")
            await screenshot("03b_login_popup_check")

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

        # Step 2: Set date — combine input value + calendar click in one call
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
            var cells = document.querySelectorAll('.ui-state-default');
            for (var cell of cells) {{
                if (cell.textContent.trim() === '{target_day}') {{
                    cell.click();
                    break;
                }}
            }}
        }}""")
        await wait_for(page, f"""() => {{
            var body = document.body.innerText;
            return body.includes('{target_date_str}');
        }}""", timeout=5, desc="date reflected")

        # Wait for tee times to reload after date change — spinner must clear
        log.info("Waiting for tee times to load after date change...")
        await wait_for(page, """() => {
            // Check page is not in loading state
            var body = document.body.innerText;
            var headerMatch = body.match(/(\\d+)\\s+tee time/i);
            var count = headerMatch ? parseInt(headerMatch[1]) : 0;
            // Wait until we have a reasonable number of times and VIEW buttons are visible
            var viewBtns = Array.from(document.querySelectorAll('a, button, span, div, label'))
                .filter(function(el) {
                    return el.textContent.trim().toUpperCase() === 'VIEW' && el.childNodes.length <= 2 && el.offsetParent !== null;
                });
            return count >= 10 && viewBtns.length >= 3 ? count : null;
        }""", timeout=15, desc="tee times loaded after date change")
        await screenshot("03b_after_date")
        elapsed = asyncio.get_event_loop().time() - start_time
        log.info(f"[{elapsed:.1f}s] Date set and tee times loaded")

        # Step 3: Set players
        log.info(f"Setting players to {NUM_PLAYERS}...")
        await js(f"""() => {{
            var btn = document.querySelector('button#players-button');
            if (!btn) {{
                btn = Array.from(document.querySelectorAll('button'))
                    .find(function(b) {{ return b.id && b.id.includes('player'); }});
            }}
            if (btn) btn.click();
        }}""")
        await wait_for(page, """() => {
            var menu = document.querySelector('ul.dropdown-menu');
            return menu && menu.offsetParent !== null;
        }""", timeout=3, desc="player dropdown")
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
        await wait_for(page, f"""() => {{
            var body = document.body.innerText;
            return body.includes('{NUM_PLAYERS} players') || body.includes('{NUM_PLAYERS} player');
        }}""", timeout=5, desc="player count reflected")

        # Wait for tee times to reload after player change
        log.info("Waiting for tee times to reload after player change...")
        await wait_for(page, """() => {
            var viewBtns = Array.from(document.querySelectorAll('a, button, span, div, label'))
                .filter(function(el) {
                    return el.textContent.trim().toUpperCase() === 'VIEW' && el.childNodes.length <= 2 && el.offsetParent !== null;
                });
            return viewBtns.length >= 3 ? viewBtns.length : null;
        }""", timeout=10, desc="tee times reloaded after players")
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
        # Wait for tee times to fully load:
        # 1. Wait for loading spinner/overlay to disappear
        # 2. Wait for multiple VIEW buttons to render (not just 1)
        log.info("Waiting for tee times to finish loading...")
        await wait_for(page, """() => {
            // Check the page isn't in a loading state (greyed out / spinner)
            var body = document.body;
            var spinner = document.querySelector('.loading, .spinner, .fa-spinner, [class*="loading"]');
            if (spinner && spinner.offsetParent !== null) return null;  // still loading

            // Check that the tee time count header has settled (not "0 tee times" or very low)
            var headerText = body.innerText.match(/(\\d+)\\s+tee time/i);
            var count = headerText ? parseInt(headerText[1]) : 0;
            if (count < 5) return null;  // still loading or throttled

            // Check that VIEW buttons have actually rendered
            var viewBtns = Array.from(document.querySelectorAll('a, button, span, div, label'))
                .filter(function(el) {
                    return el.textContent.trim().toUpperCase() === 'VIEW' && el.childNodes.length <= 2 && el.offsetParent !== null;
                });
            return viewBtns.length >= 3 ? viewBtns.length : null;
        }""", timeout=20, desc="tee times fully loaded")

        # Final check — how many VIEW buttons do we have?
        view_count = await js("""() => {
            var viewBtns = Array.from(document.querySelectorAll('a, button, span, div, label'))
                .filter(function(el) {
                    return el.textContent.trim().toUpperCase() === 'VIEW' && el.childNodes.length <= 2 && el.offsetParent !== null;
                });
            var headerText = document.body.innerText.match(/(\\d+)\\s+tee time/i);
            return JSON.stringify({viewButtons: viewBtns.length, headerCount: headerText ? headerText[1] : '?'});
        }""")
        log.info(f"Page load check: {view_count}")

        await screenshot("03d_after_pricing")
        elapsed = asyncio.get_event_loop().time() - start_time
        log.info(f"[{elapsed:.1f}s] Pricing option set, tee times loaded")

        await js("() => { window.scrollTo(0, 0); }")

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
            # Poll by re-triggering the date (Angular refresh) until morning times appear
            # No full page reload — just nudge the date back and forth to refresh tee times
            target_minutes = time_to_minutes(TARGET_TIME)
            poll_attempt = 0
            MAX_POLL_ATTEMPTS = 3600  # ~60 minutes at ~1s intervals
            quick_check = None

            while poll_attempt < MAX_POLL_ATTEMPTS:
                poll_attempt += 1
                now = datetime.now()
                if poll_attempt <= 5 or poll_attempt % 30 == 0:
                    log.info(f"Poll attempt {poll_attempt} at {now.strftime('%H:%M:%S.%f')[:-3]}...")

                # Re-trigger date to refresh tee times without full reload
                await js(f"""() => {{
                    var input = document.querySelector('input#pickerDate, input[datepicker]');
                    if (input) {{
                        // Nudge to different date then back to force Angular refresh
                        input.value = '';
                        input.dispatchEvent(new Event('input', {{bubbles: true}}));
                        input.dispatchEvent(new Event('change', {{bubbles: true}}));
                        try {{ angular.element(input).triggerHandler('change'); }} catch(e) {{}}
                    }}
                }}""")
                await asyncio.sleep(0.3)
                await js(f"""() => {{
                    var input = document.querySelector('input#pickerDate, input[datepicker]');
                    if (input) {{
                        input.value = '{target_date_str}';
                        input.dispatchEvent(new Event('input', {{bubbles: true}}));
                        input.dispatchEvent(new Event('change', {{bubbles: true}}));
                        try {{ angular.element(input).triggerHandler('change'); }} catch(e) {{}}
                    }}
                    var cells = document.querySelectorAll('.ui-state-default');
                    for (var cell of cells) {{
                        if (cell.textContent.trim() === '{target_day}') {{
                            cell.click();
                            break;
                        }}
                    }}
                }}""")
                await asyncio.sleep(1)

                # Quick scrape — check if morning times exist
                quick_check = await js("""() => {
                    var body = document.body.innerText;
                    var morningTimes = body.match(/\\b[5-9]:\\d{2}\\s*AM|\\b1[0-1]:\\d{2}\\s*AM/gi);
                    return morningTimes ? morningTimes.join(',') : '';
                }""")

                if quick_check:
                    log.info(f"Morning times detected: {quick_check}")
                    await js("() => { window.scrollTo(0, 0); }")
                    await asyncio.sleep(0.5)
                    break
                else:
                    log.info("  No morning times yet...")

            if not quick_check:
                log.warning(f"No morning times appeared after {MAX_POLL_ATTEMPTS} poll attempts — falling back to best available time")
                await screenshot("04_poll_exhausted_fallback")

            if quick_check:
                log.info("Morning times are live! Proceeding to scrape and book...")
            else:
                log.info("Proceeding with best available time (fallback)...")
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
            var seen = {};

            function addTime(time, maxP, minP, price) {
                if (seen[time]) return;
                var parts = time.match(/(\\d+):(\\d+)/);
                if (!parts) return;
                var h = parseInt(parts[1]), m = parseInt(parts[2]);
                if (h < 1 || h > 12 || m < 0 || m > 59) return;
                seen[time] = true;
                results.push({ time: time, maxPlayers: maxP, minPlayers: minP, price: price });
            }

            // Strategy 0: Angular scope — may return paginated subset
            try {
                var ngEls = document.querySelectorAll('[ng-repeat], [data-ng-repeat]');
                for (var ngIdx = 0; ngIdx < ngEls.length; ngIdx++) {
                    var scope = angular.element(ngEls[ngIdx]).scope();
                    if (!scope) continue;
                    // Walk up scope chain looking for tee time arrays
                    var s = scope;
                    for (var depth = 0; depth < 5 && s; depth++) {
                        var keys = Object.keys(s);
                        for (var k = 0; k < keys.length; k++) {
                            var val = s[keys[k]];
                            if (Array.isArray(val) && val.length > 0 && val[0] && (val[0].time || val[0].teeTime || val[0].startTime)) {
                                val.forEach(function(tt) {
                                    var timeStr = tt.time || tt.teeTime || tt.startTime || '';
                                    if (timeStr) {
                                        addTime(timeStr, tt.maxPlayers || tt.max_players || 4, tt.minPlayers || tt.min_players || 1, tt.price || tt.greenFee || 'N/A');
                                    }
                                });
                            }
                        }
                        s = s.$parent;
                    }
                    if (results.length > 0) break;
                }
            } catch(e) {}

            // Strategy 1: Find VIEW buttons (any element type) and walk up to tee time cards
            var viewBtns = Array.from(document.querySelectorAll('a, button, span, div, label'))
                .filter(function(el) {
                    return el.textContent.trim().toUpperCase() === 'VIEW' && el.childNodes.length <= 2;
                });

            viewBtns.forEach(function(btn) {
                var container = btn.closest('.panel, .card, .col-md-4, .col-sm-4, .col-lg-4, [ng-repeat], [data-ng-repeat]');
                if (!container) {
                    container = btn.parentElement;
                    for (var i = 0; i < 6 && container; i++) {
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
                    addTime(timeMatch[1], playerMatch ? parseInt(playerMatch[2]) : 4, playerMatch ? parseInt(playerMatch[1]) : 1, priceMatch ? priceMatch[0] : 'N/A');
                }
            });

            // Strategy 2: ng-repeat elements (fallback if above found nothing)
            if (results.length === 0) {
                var ngRepeats = document.querySelectorAll('[ng-repeat], [data-ng-repeat]');
                ngRepeats.forEach(function(el) {
                    var text = el.innerText || '';
                    var timeMatch = text.match(/(\\d{1,2}:\\d{2}\\s*[AP]M)/i);
                    if (timeMatch) {
                        var playerMatch = text.match(/(\\d+)[\\u2013-](\\d+)\\s*Player/i);
                        var priceMatch = text.match(/\\$(\\d+\\.\\d+)/);
                        addTime(timeMatch[1], playerMatch ? parseInt(playerMatch[2]) : 4, playerMatch ? parseInt(playerMatch[1]) : 1, priceMatch ? priceMatch[0] : 'N/A');
                    }
                });
            }

            return JSON.stringify(results);
        }"""

        log.info("Scraping tee times (scrolling to collect all)...")

        # Scroll through the page to load all tee times, break early when done
        all_times = set()
        all_results = []
        no_new_streak = 0
        for scroll_pass in range(10):
            raw = await js(SCRAPE_JS)
            if isinstance(raw, str):
                batch = json.loads(raw)
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
            if new_count == 0:
                no_new_streak += 1
                if no_new_streak >= 2:
                    log.info("  No new times for 2 passes, done scraping")
                    break
            else:
                no_new_streak = 0
            await js("() => { window.scrollBy(0, 800); }")
            await asyncio.sleep(0.3)

        # Use collected results instead of single scrape
        raw_result = json.dumps(all_results)
        log.info(f"Total unique tee times collected: {len(all_results)}")
        log.info(f"Raw scrape result type: {type(raw_result)}, preview: {str(raw_result)[:200]}")

        # CDP evaluate may return the data in different formats
        if isinstance(raw_result, str):
            tee_times = json.loads(raw_result)
        elif isinstance(raw_result, list):
            tee_times = raw_result
        else:
            tee_times = raw_result

        if not tee_times:
            log.warning("No tee times found, retrying in 3s...")
            await asyncio.sleep(3)
            raw_result = await js(SCRAPE_JS)
            if isinstance(raw_result, str):
                tee_times = json.loads(raw_result)
            elif isinstance(raw_result, list):
                tee_times = raw_result
            else:
                tee_times = raw_result

        if not tee_times:
            log.warning("No tee times found after retry — waiting 10s and trying once more...")
            await asyncio.sleep(10)
            # Re-trigger date to force refresh
            await js(f"""() => {{
                var input = document.querySelector('input#pickerDate, input[datepicker]');
                if (input) {{
                    input.value = '{target_date_str}';
                    input.dispatchEvent(new Event('input', {{bubbles: true}}));
                    input.dispatchEvent(new Event('change', {{bubbles: true}}));
                    try {{ angular.element(input).triggerHandler('change'); }} catch(e) {{}}
                }}
            }}""")
            await asyncio.sleep(5)
            raw_result = await js(SCRAPE_JS)
            if isinstance(raw_result, str):
                tee_times = json.loads(raw_result)
            elif isinstance(raw_result, list):
                tee_times = raw_result
            else:
                tee_times = raw_result

        if not tee_times:
            log.error("No tee times found after all retries")
            await screenshot("debug_no_times")
            sys.exit(1)

        # Ensure tee_times is a list of dicts
        if isinstance(tee_times, list) and tee_times and isinstance(tee_times[0], str):
            log.warning(f"Tee times returned as strings, attempting to parse")
            tee_times = [json.loads(t) if isinstance(t, str) else t for t in tee_times]

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
            log.warning(f"No tee times with {NUM_PLAYERS} player slots — trying with fewer players")
            best = await find_best_tee_time(tee_times, TARGET_TIME, 1)
        if not best:
            log.error("No tee times found at all")
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

        # ---------------------------------------------------------------
        # BOOKING FLOW WITH RETRY (up to 30 attempts)
        # "Tee Time Adjustment" can appear at ANY step in the flow.
        # On snipe: dismiss dialog, pick next best time, restart flow.
        # ---------------------------------------------------------------
        tried_times = set()
        MAX_ATTEMPTS = 30
        target_minutes = time_to_minutes(TARGET_TIME)

        async def wait_for_page_change(keyword, timeout=6):
            """Poll rapidly for a keyword or dialog to appear. Returns page text."""
            for _ in range(timeout * 4):  # poll every 250ms
                text = await js("() => document.body.innerText.substring(0, 2000)")
                if keyword in text or "Tee Time Adjustment" in text or "no longer available" in text:
                    return text
                await asyncio.sleep(0.25)
            return await js("() => document.body.innerText.substring(0, 2000)")

        async def dismiss_dialogs():
            """Dismiss OK/attention dialogs, then check for Tee Time Adjustment. Returns 'sniped', 'already_booked', or False."""
            await js("""() => {
                var buttons = document.querySelectorAll('button');
                for (var btn of buttons) {
                    if (btn.textContent.trim() === 'OK' && btn.offsetParent !== null) btn.click();
                }
            }""")
            await asyncio.sleep(0.3)
            page_text = await js("() => document.body.innerText.substring(0, 2000)")

            # Check for existing reservation on this date
            if "already" in page_text.lower() and ("reservation" in page_text.lower() or "booked" in page_text.lower() or "tee time" in page_text.lower()):
                log.info("=" * 60)
                log.info("Already have a reservation for this date — stopping gracefully")
                log.info("=" * 60)
                return "already_booked"

            if "Tee Time Adjustment" in page_text or "no longer available" in page_text:
                log.warning("Tee Time Adjustment detected — time was sniped!")
                await js("""() => {
                    var buttons = document.querySelectorAll('button');
                    for (var btn of buttons) {
                        if (btn.textContent.trim() === 'No' && btn.offsetParent !== null) btn.click();
                    }
                }""")
                await asyncio.sleep(1)
                return "sniped"
            return False

        async def ensure_on_search_page():
            """Make sure we're on the tee time search page before retrying."""
            page_text = await js("() => document.body.innerText.substring(0, 1000)")
            if "VIEW" in page_text and "Pricing Options" in page_text:
                return  # Already on search page
            log.info("Navigating back to search page...")
            # Close any open modals first
            await js("""() => {
                var closeBtns = document.querySelectorAll('.modal .close, .modal [data-dismiss="modal"], .modal button.close');
                for (var btn of closeBtns) {
                    if (btn.offsetParent !== null) btn.click();
                }
            }""")
            await asyncio.sleep(0.5)
            # Try clicking Cancel or Previous to go back
            await js("""() => {
                var links = document.querySelectorAll('a, button');
                for (var el of links) {
                    var text = el.textContent.trim().toLowerCase();
                    if (text.includes('cancel') || text === '< previous' || text === 'back') {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""")
            await asyncio.sleep(1)
            # Verify we're back — if not, use the URL hash
            page_text = await js("() => document.body.innerText.substring(0, 1000)")
            if "VIEW" not in page_text:
                await js("() => { window.location.hash = '#/search'; }")
                await asyncio.sleep(2)
            await js("() => { window.scrollTo(0, 0); }")

        async def pick_next_best():
            """Pick the next best time after a snipe. Returns False if none left."""
            nonlocal best
            tried_times.add(best['time'])
            available = [t for t in tee_times if t['time'] not in tried_times]
            if not available:
                log.error("No more times to try!")
                return False
            best = min(available, key=lambda t: abs(time_to_minutes(t['time']) - target_minutes))
            log.info(f"Next best time: {best['time']}")
            await ensure_on_search_page()
            return True

        for attempt in range(MAX_ATTEMPTS):
            log.info("=" * 60)
            log.info(f"BOOKING ATTEMPT {attempt + 1}/{MAX_ATTEMPTS}: {best['time']}")
            log.info("=" * 60)

            sniped = False

            # Step 6: Scroll to the target time's VIEW button and click it
            # First, try to scroll the target time into view, then match
            log.info(f"Clicking VIEW for {best['time']}...")

            # Try to scroll the target time card into view first
            await js(f"""() => {{
                var allEls = document.querySelectorAll('*');
                for (var el of allEls) {{
                    if (el.childNodes.length <= 3 && el.textContent.trim() === '{best["time"]}') {{
                        el.scrollIntoView({{behavior: 'instant', block: 'center'}});
                        return true;
                    }}
                }}
                window.scrollTo(0, 0);
                return false;
            }}""")
            await asyncio.sleep(0.3)

            clicked = await js(f"""() => {{
                var viewEls = [];
                var allEls = document.querySelectorAll('a, button, span, div, label');
                for (var el of allEls) {{
                    var t = el.textContent.trim().toUpperCase();
                    if (t === 'VIEW') viewEls.push(el);
                }}
                for (var btn of viewEls) {{
                    var parent = btn;
                    for (var i = 0; i < 10 && parent; i++) {{
                        if (parent.innerText && parent.innerText.includes('{best["time"]}')) {{
                            btn.click();
                            return 'found_' + btn.tagName;
                        }}
                        parent = parent.parentElement;
                    }}
                }}
                return null;
            }}""")

            if not clicked:
                log.warning(f"Could not match VIEW to '{best['time']}' — skipping to next best time")
                if not await pick_next_best():
                    break
                continue

            log.info(f"VIEW clicked via '{clicked}'")
            await wait_for_page_change("CHOOSE OPTION", timeout=5)
            await screenshot(f"06_attempt{attempt+1}")

            # Check for snipe after VIEW
            dialog_result = await dismiss_dialogs()
            if dialog_result == "already_booked":
                sys.exit(0)
            if dialog_result:
                if not await pick_next_best():
                    break
                continue

            # Step 7: Select Member Walk 18H
            log.info("Selecting Member Walk 18H...")
            await js("""() => {
                var modal = document.querySelector('.modal-open') || document.body;
                var ngClickEls = modal.querySelectorAll('[ng-click], [data-ng-click]');
                for (var el of ngClickEls) {
                    var text = el.textContent.trim();
                    if (text.includes('Member Walk 18H') && !text.includes('Ride') && !text.includes('CHOOSE')) {
                        el.click();
                        return;
                    }
                }
                var allEls = modal.querySelectorAll('*');
                for (var el of allEls) {
                    if (el.childNodes.length <= 2 && el.textContent.trim() === 'Member Walk 18H') {
                        var row = el.closest('tr, li, [ng-click], [data-ng-click], a, button');
                        if (row) { row.click(); return; }
                        el.parentElement.click();
                        return;
                    }
                }
            }""")
            await asyncio.sleep(1)

            if DEBUG:
                modal_state = await js("""() => {
                    var modal = document.querySelector('.modal.in, .modal.show, .modal[style*="display: block"]');
                    if (!modal) return JSON.stringify({error: 'no modal found'});
                    var selected = modal.querySelector('.selected, .active, .highlighted, tr.info, tr.success, [class*="selected"]');
                    var continueBtn = null;
                    var buttons = modal.querySelectorAll('button, a, input[type="submit"]');
                    for (var btn of buttons) {
                        if (btn.textContent.trim() === 'Continue' && btn.offsetParent !== null) {
                            continueBtn = {text: btn.textContent.trim(), disabled: btn.disabled, tag: btn.tagName, classes: btn.className};
                        }
                    }
                    var selects = modal.querySelectorAll('select');
                    var selectInfo = [];
                    for (var s of selects) {
                        selectInfo.push({id: s.id, name: s.name, value: s.value, options: Array.from(s.options).map(o => o.text.trim())});
                    }
                    return JSON.stringify({
                        selectedEl: selected ? selected.innerText.substring(0, 100) : null,
                        continueBtn: continueBtn,
                        selects: selectInfo,
                        modalText: modal.innerText.substring(0, 500)
                    });
                }""")
                log.info(f"Modal state after pricing selection: {modal_state}")
            await screenshot(f"06b_after_pricing_select_attempt{attempt+1}")

            # Set 4 players in modal
            log.info(f"Setting modal players to {NUM_PLAYERS}...")
            await js(f"""() => {{
                var selects = document.querySelectorAll('select');
                for (var sel of selects) {{
                    var opts = Array.from(sel.options).map(o => o.text.trim());
                    if (opts.includes('1 player') || opts.includes('2 players')) {{
                        for (var opt of sel.options) {{
                            if (opt.text.trim() === '{NUM_PLAYERS} players' || opt.text.trim() === '{NUM_PLAYERS}') {{
                                sel.value = opt.value;
                                sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                                try {{ angular.element(sel).triggerHandler('change'); }} catch(e) {{}}
                                return;
                            }}
                        }}
                    }}
                }}
            }}""")
            await asyncio.sleep(0.5)

            if DEBUG:
                modal_state2 = await js("""() => {
                    var modal = document.querySelector('.modal.in, .modal.show, .modal[style*="display: block"]');
                    if (!modal) return JSON.stringify({error: 'no modal found'});
                    var buttons = modal.querySelectorAll('button, a, input[type="submit"]');
                    var allBtns = [];
                    for (var btn of buttons) {
                        if (btn.offsetParent !== null) {
                            allBtns.push({text: btn.textContent.trim(), disabled: btn.disabled, tag: btn.tagName, classes: btn.className.substring(0, 60)});
                        }
                    }
                    return JSON.stringify({buttons: allBtns, modalText: modal.innerText.substring(0, 500)});
                }""")
                log.info(f"Modal state after players: {modal_state2}")
            await screenshot(f"06c_after_players_attempt{attempt+1}")

            # Click Continue in CHOOSE OPTIONS modal — single clean click only
            log.info("Clicking Continue...")
            continue_result = await js("""() => {
                var buttons = document.querySelectorAll('button, a, input[type="submit"]');
                for (var btn of buttons) {
                    if (btn.textContent.trim() === 'Continue' && btn.offsetParent !== null) {
                        btn.click();
                        return 'clicked_' + btn.tagName + '_' + btn.className;
                    }
                }
                return 'no_button_found';
            }""")
            log.info(f"Continue click result: {continue_result}")

            # Give the page time to transition — Verify Details takes 3-5 seconds to load
            page_after_continue = await wait_for_page_change("Verify Details", timeout=10)
            await screenshot(f"06d_after_continue_attempt{attempt+1}")

            # Check for snipe after Continue
            dialog_result = await dismiss_dialogs()
            if dialog_result == "already_booked":
                sys.exit(0)
            if dialog_result:
                if not await pick_next_best():
                    break
                continue

            # Check where we landed after Continue
            page_state = await js("""() => {
                var body = document.body.innerText.substring(0, 2000);
                if (body.includes('Verify Details')) return 'verify_details';
                if (body.includes('Finish') && body.includes('Reservation')) return 'finish';
                var modal = document.querySelector('.modal.in, .modal.show, .modal[style*="display: block"]');
                if (modal && modal.offsetParent !== null && modal.innerText.includes('CHOOSE OPTION')) return 'still_in_modal';
                if (body.includes('Sign In') && !body.includes('Sign Out') && !body.includes('My Account')) return 'logged_out';
                return 'unknown: ' + body.substring(0, 200);
            }""")
            log.info(f"Page state after Continue: {page_state}")

            if page_state == 'logged_out':
                log.error("Session lost — logged out after Continue click")
                sys.exit(1)

            if page_state == 'still_in_modal':
                log.warning(f"Still in CHOOSE OPTIONS modal after Continue — skipping to next time.")
                await js("""() => {
                    var closeBtn = document.querySelector('.modal .close, .modal [data-dismiss="modal"], .modal button.close');
                    if (closeBtn && closeBtn.offsetParent !== null) closeBtn.click();
                }""")
                await asyncio.sleep(0.5)
                if not await pick_next_best():
                    break
                continue

            if page_state.startswith('unknown'):
                # Page might still be loading — wait a bit more
                log.info("Page still loading, waiting...")
                await asyncio.sleep(3)
                page_state = await js("""() => {
                    var body = document.body.innerText.substring(0, 2000);
                    if (body.includes('Verify Details')) return 'verify_details';
                    if (body.includes('Finish') && body.includes('Reservation')) return 'finish';
                    return 'still_unknown: ' + body.substring(0, 200);
                }""")
                log.info(f"Page state after extra wait: {page_state}")

            if not page_state.startswith('verify') and not page_state.startswith('finish'):
                log.warning(f"Unexpected page state: {page_state} — skipping to next time")
                if not await pick_next_best():
                    break
                continue

            # Step 8: Click CONTINUE on Verify Details page
            # Dismiss any stale modal overlays first
            await js("""() => {
                var closeBtn = document.querySelector('.modal .close, .modal [data-dismiss="modal"], .modal button.close');
                if (closeBtn && closeBtn.offsetParent !== null) closeBtn.click();
            }""")
            await asyncio.sleep(0.3)

            log.info("Clicking CONTINUE on Verify Details...")
            await js("""() => {
                // Click the LAST visible Continue — the one on Verify Details, not a stale modal
                var buttons = document.querySelectorAll('button, a, input[type="submit"]');
                var lastContinue = null;
                for (var btn of buttons) {
                    var text = btn.textContent.trim();
                    if ((text === 'Continue' || text === 'CONTINUE') && btn.offsetParent !== null) {
                        lastContinue = btn;
                    }
                }
                if (lastContinue) lastContinue.click();
            }""")
            page_after_verify = await wait_for_page_change("Finish", timeout=5)

            # Check for snipe after Verify Details
            dialog_result = await dismiss_dialogs()
            if dialog_result == "already_booked":
                sys.exit(0)
            if dialog_result:
                if not await pick_next_best():
                    break
                continue

            # Verify we actually reached the Finish page
            if "Finish" not in page_after_verify and "Pricing Options" in page_after_verify:
                log.warning("Bounced back to Pricing Options after Verify Details Continue — retrying with next time")
                if not await pick_next_best():
                    break
                continue

            # Step 9: Click Finish Reservation
            final_content = await js("() => document.body.innerText.substring(0, 1500)")
            log.info(f"Final page:\n{final_content}")

            # Verify we're actually on the Finish page
            has_finish = await js("""() => {
                var buttons = document.querySelectorAll('a, button, input[type="submit"]');
                var bookWords = ['book', 'reserve', 'confirm', 'complete', 'submit', 'finish'];
                for (var btn of buttons) {
                    var text = btn.textContent.trim().toLowerCase();
                    if (btn.offsetParent === null) continue;
                    for (var word of bookWords) {
                        if (text.includes(word) && !text.includes('cancel')) {
                            return text;
                        }
                    }
                }
                return null;
            }""")

            if not has_finish:
                log.warning("Could not find final booking button")
                await screenshot(f"09_attempt{attempt+1}_no_book")
                dialog_result = await dismiss_dialogs()
                if dialog_result == "already_booked":
                    sys.exit(0)
                if not await pick_next_best():
                    log.error("No more times to try — giving up")
                    break
                continue

            if DRY_RUN:
                log.info("=" * 60)
                log.info(f"DRY RUN — stopping before Finish Reservation")
                log.info(f"Would book: {best['time']} on {target_day_str}")
                log.info(f"Players: {NUM_PLAYERS}")
                log.info(f"Final button found: '{has_finish}'")
                log.info("=" * 60)
                await screenshot("09_dry_run_finish_page")
                # Cancel to back out cleanly
                await js("""() => {
                    var links = document.querySelectorAll('a, button');
                    for (var el of links) {
                        var text = el.textContent.trim().toLowerCase();
                        if (text.includes('cancel') || text === '< previous') {
                            el.click();
                            return;
                        }
                    }
                }""")
                await asyncio.sleep(2)
                break

            log.info("Looking for final booking button...")
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
                await wait_for_page_change("Reservation Complete", timeout=5)
                await screenshot("09_after_book_click")

                # Verify it actually worked (check for confirmation)
                confirmation = await js("() => document.body.innerText.substring(0, 2000)")
                if "Reservation Complete" in confirmation or "Confirmation" in confirmation:
                    log.info("=" * 60)
                    log.info(f"BOOKING SUCCESSFUL!")
                    log.info(f"Time: {best['time']} on {target_day_str}")
                    log.info(f"Players: {NUM_PLAYERS}")
                    log.info("=" * 60)
                    log.info(f"Page content:\n{confirmation}")
                    break  # SUCCESS!
                elif "Tee Time Adjustment" in confirmation or "no longer available" in confirmation:
                    log.warning("Sniped AFTER clicking Finish!")
                    await js("""() => {
                        var buttons = document.querySelectorAll('button');
                        for (var btn of buttons) {
                            if (btn.textContent.trim() === 'No' && btn.offsetParent !== null) btn.click();
                        }
                    }""")
                    await asyncio.sleep(1)
                    if not await pick_next_best():
                        break
                    continue
                else:
                    # Assume success if no error detected
                    log.info("=" * 60)
                    log.info(f"BOOKING FLOW COMPLETED!")
                    log.info(f"Time: {best['time']} on {target_day_str}")
                    log.info("=" * 60)
                    log.info(f"Page content:\n{confirmation}")
                    break
        else:
            # Exhausted all attempts
            log.error(f"All {MAX_ATTEMPTS} booking attempts failed!")
            await screenshot("09_all_attempts_failed")
            sys.exit(1)

    except Exception as e:
        log.exception(f"Error: {e}")
        await screenshot("error")
        sys.exit(1)
    finally:
        await asyncio.sleep(3)
        await browser_session.stop()


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore", category=ResourceWarning, message="unclosed transport")
    asyncio.run(main())
