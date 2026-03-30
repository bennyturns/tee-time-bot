# Tee Time Bot

Automated tee time booking bot for golf courses running the EZLinks platform. Built for speed — pre-logs in before the booking window opens, then grabs the best available time the instant new slots drop.

## How It Works

The bot uses a hybrid AI + browser automation approach:

1. **6:57 AM** — Bot launches, navigates to the booking site
2. **Cloudflare bypass** — AI agent (Claude Sonnet + vision) clicks through the "Verify you are human" challenge
3. **Login & setup** — Direct CDP (Chrome DevTools Protocol) calls handle login, date selection, player count, and pricing option — much faster than AI for known UI flows
4. **Wait for drop** — Bot sits pre-positioned, counting down to 7:00 AM
5. **7:00 AM** — Instantly refreshes, scrapes all available times, picks the closest to your target, and books it

Total time from drop to booked: **~15 seconds**.

## Architecture

```
browser-use (AI agent)          nodriver (CDP)
       |                              |
  Cloudflare bypass            Login, date, players,
  (~60-90 seconds)             scrape, click, book
                               (~15 seconds)
```

- **Phase 1**: `browser-use` with Claude Sonnet vision handles the Cloudflare Turnstile captcha — the one thing that requires human-like interaction
- **Phase 2**: Direct JavaScript execution via CDP for everything else — no AI overhead, just fast DOM manipulation

## Setup

### Prerequisites

- Python 3.12 (3.14 breaks nodriver)
- Anthropic API key
- Chrome/Chromium browser

### Install

```bash
git clone https://github.com/bennyturns/tee-time-bot.git
cd tee-time-bot
pip install browser-use python-dotenv
```

### Configure

```bash
cp .env.example .env
# Edit .env with your credentials
```

`.env` settings:

| Variable | Description | Default |
|----------|-------------|---------|
| `EZLINKS_USERNAME` | EZLinks login username | (required) |
| `EZLINKS_PASSWORD` | EZLinks login password | (required) |
| `ANTHROPIC_API_KEY` | Claude API key for Cloudflare bypass | (required) |
| `BOOKING_URL` | Golf course EZLinks URL | `https://lochmeregm.ezlinksgolf.com` |
| `TARGET_TIME` | Preferred tee time (closest match) | `08:28` |
| `NUM_PLAYERS` | Number of players | `4` |
| `DAYS_OUT` | Days in advance to book | `14` |
| `BOOKING_HOUR` | Hour when new times drop | `7` |
| `BOOKING_MINUTE` | Minute when new times drop | `0` |

### Run manually

```bash
python3.12 book_tee_time_fast.py

# Dry run (finds best time but doesn't book)
python3.12 book_tee_time_fast.py --dry-run
```

### Run on a schedule (systemd)

```bash
# Copy unit files
mkdir -p ~/.config/systemd/user
cp tee-time-bot.service ~/.config/systemd/user/
cp tee-time-bot.timer ~/.config/systemd/user/

# Enable linger (allows timers to run when not logged in)
loginctl enable-linger $USER

# Start the timer
systemctl --user daemon-reload
systemctl --user enable --now tee-time-bot.timer

# Check status
systemctl --user status tee-time-bot.timer
```

The timer fires at **6:57 AM daily** — 3 minutes before the 7:00 drop — giving the bot time to pass Cloudflare and login before new times go live.

### Logs

```bash
# View recent run
tail -100 ~/Workspace/tee-time-bot/cron_booking.log

# Watch live
journalctl --user -u tee-time-bot.service -f
```

## How Time Selection Works

The bot picks the tee time **closest to your target time** that supports your player count:

- Scrolls through all available times (10 scroll passes to load lazy-rendered cards)
- Filters for times with enough player slots
- Slightly prefers times before the target over after
- Books the "Member Walk 18H" pricing option automatically
- If no times within range have enough slots, it reports no suitable times

## Adapting for Other Courses

This bot works with any EZLinks-powered golf course. To adapt:

1. Change `BOOKING_URL` to your course's EZLinks URL
2. Update `TARGET_TIME`, `NUM_PLAYERS`, and `DAYS_OUT` as needed
3. Update `BOOKING_HOUR`/`BOOKING_MINUTE` if your course drops times at a different time
4. The pricing option selector may need adjustment if your course uses different names

## License

MIT
