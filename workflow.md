# Tee Time Bot Workflow

```
 7:26 AM                    7:29:45 AM              7:30 AM
    |                           |                      |
    v                           v                      v
+--------+    +-------+    +---------+    +--------+    +---------+
| START  |--->| CLOUD-|--->| LOGIN & |--->| WAIT   |--->| RAPID   |
| Bot    |    | FLARE |    | SETUP   |    | ON     |    | POLL    |
| Starts |    | PASS  |    |         |    | PAGE   |    | REFRESH |
+--------+    +-------+    +---------+    +--------+    +---------+
                                                             |
   ~60s AI        ~10s          ~15s        ~2.5 min         | Page refresh
   clicks         sign in       set date,                    | every ~7s
   checkbox       via CDP       players,                     | looking for
                                pricing                      | morning times
                                                             |
                                                             v
                                                   +------------------+
                                                   | MORNING TIMES    |
                                                   | DETECTED!        |
                                                   +------------------+
                                                             |
                                                             v
                                                   +------------------+
                                                   | SCRAPE ALL TIMES |
                                                   | Find closest to  |
                                                   | 8:28 AM          |
                                                   +------------------+
                                                             |
                                                             v
                                                   +------------------+
                                                   | CLICK VIEW       |
                                                   +------------------+
                                                             |
                                              +--------------+--------------+
                                              |                             |
                                              v                             v
                                     +-----------------+          +------------------+
                                     | TIME AVAILABLE  |          | "TEE TIME        |
                                     | Select Walk 18H |          |  ADJUSTMENT"     |
                                     | Set 4 players   |          | Time was sniped! |
                                     | Click CONTINUE  |          +------------------+
                                     +-----------------+                    |
                                              |                             v
                                              v                    +-----------------+
                                     +-----------------+           | Click "No"      |
                                     | VERIFY DETAILS  |           | Try next best   |
                                     | Click CONTINUE  |           | time (up to 5x) |
                                     +-----------------+           +-----------------+
                                              |                             |
                                              v                    loops back to
                                     +-----------------+           CLICK VIEW
                                     | FINISH          |
                                     | RESERVATION     |
                                     +-----------------+
                                              |
                                              v
                                     +-----------------+
                                     | BOOKED!         |
                                     | Confirmation #  |
                                     +-----------------+


FAILURE HANDLING:
- Any failure exits with code 1 (systemd marks as failed)
- Tee time sniped? Retry up to 5 times with next closest time
- No morning times after 60 polls? Exit with failure
- Can't find booking button? Exit with failure
- Exception? Exit with failure


TIMELINE:
  7:26:00  Bot starts, Cloudflare + login (~70s)
  7:27:10  Date/players/pricing set (~15s)
  7:27:25  Waiting on page...
  7:29:45  Start rapid polling (refresh every ~7s)
  7:30:00  New times drop - detected on next poll
  7:30:07  Scraping + clicking VIEW
  7:30:15  Booking confirmed (best case)
```
