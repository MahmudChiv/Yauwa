# Rehearsing Musa's demo

The demo uses the 12 recordings in [SCRIPT.md](SCRIPT.md), with [script.json](script.json) as the source of truth. Save each MP3 under `demo/audio/` with its exact filename. No audio is generated at runtime. Files must be nonempty. Include these files in the deployed build.

Set `DEMO_MODE=true` and `DEMO_PHONE_NUMBER=+2348164247735` locally and in Railway. Existing Twilio credentials, WhatsApp sender, webhook URL, and public base URL are still required. Public media and webhook URLs must share an HTTPS origin. Local `.env` does not configure Railway.

Use **one worker and one replica**: progress and duplicate tracking are process-local. Start the app, check [the demo status](https://yauwa-production.up.railway.app/demo/status), then open [the demo reset link](https://yauwa-production.up.railway.app/demo/reset) before rehearsing. Both endpoints are unauthenticated and exist only with demo mode enabled. Restarting resets progress; restart after editing JSON. Reset rechecks files but does not reload JSON or reset any real trader data.

Send six separate messages from Musa's number, waiting for all replies before sending the next. Message content is ignored, but these inputs make the story consistent:

| Step | Trader input | Reply files |
|---|---|---:|
| 0 | Introduce yourself as Musa | 1 |
| 1 | Four cartons of biscuits (10 each); three bags of water (20 each); six rolls of Dano (10 each); six packs of diapers (5 each); 30 bread; two packs each of Fanta and Maltina (6 each) | 1 |
| 2 | Sold 37 biscuits | 2 |
| 3 | Sold 55 Dano, 28 bread, 11 Fanta, 11 Maltina | 2 |
| 4 | I wan go market | 5 |
| 5 | I don buy the goods for market | 1 |

Alerts precede sale confirmations, with one second between files. Pure water and diapers remain untouched. Exact top-ups restore opening quantities; choosing the suggested rounded full packages can exceed those quantities. Nothing is written to inventory or the ledger.

[`/demo/status`](https://yauwa-production.up.railway.app/demo/status) reports readiness, missing files, the next step index, completion, and delivery failures. Missing files block demo messages with HTTP 503 without consuming their message SID. A delivery failure pauses playback; inspect the Twilio logs, resolve it, then use [`/demo/reset`](https://yauwa-production.up.railway.app/demo/reset) for a fresh rehearsal. Already accepted audios cannot be recalled. Do not reset while messages are queued. After step 5, additional messages send nothing until reset.

Twilio acceptance is not proof of handset delivery. Twilio, public hosting, and their network connections remain necessary. Other callers still share the application's process and use the real pipeline. Disabling demo mode with `DEMO_MODE=false` and restarting restores normal handling for Musa too.

## After the hackathon — separate production branch

- Combine multiple low-stock alerts from one bulk sale into a single audio, separate from the sale confirmation.
- Calculate market-list quantities using stored package sizes, naming cartons/bags/rolls/packs plus remaining pieces, with optional full-package rounding.

These production changes are not part of the demo implementation.
