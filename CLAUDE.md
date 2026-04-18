# Tee Time Bot - Development Rules

## Rule #1: Look like a human

This bot must behave exactly like a person booking a tee time in a browser. Anything that looks automated will get us flagged, throttled, or banned.

- **One tab, one session, one browser.** Never open multiple tabs or concurrent sessions.
- **Never run multiple tests back-to-back.** The site throttles rapid sequential sessions and revokes tokens. One test at a time, with gaps between runs.
- **No rapid-fire actions.** A human doesn't click 10 buttons in 500ms. Keep realistic pacing between interactions.
- **Don't retry aggressively.** If a time gets sniped, a human picks another and tries — they don't machine-gun through every slot in 2 seconds.
- **One booking attempt per morning.** The cron job runs once at drop time. If it fails, it fails. Don't auto-retry the whole flow.

## Rule #2: Diagnose before you fix

When a run fails, do NOT immediately change the code. Follow this process:

1. **Read the logs together.** Walk through what happened step by step.
2. **Identify the root cause.** Not the symptom — the actual cause.
3. **Re-evaluate the full script.** Understand how the fix interacts with everything else.
4. **Think critically.** Will this fix break something else? Is there a simpler approach? Are we adding complexity that makes the next failure harder to debug?
5. **Discuss the plan.** Propose the fix, get alignment, then implement.

Rapid-fire patching has introduced more bugs than it's fixed. Every "quick fix" that skips this process risks making things worse.

## Testing

- Run 1 dry-run test at a time. Never batch tests.
- Wait between test runs — at least a few minutes.
- Prefer testing during off-peak hours to avoid competing for real slots.

## Tech stack

- Python 3.12, browser-use + nodriver for Cloudflare, then direct CDP for speed
- EZLinks Angular booking site, Cloudflare protected
- Creds in .env (never commit)
