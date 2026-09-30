# Guide for teammates and their coding agents

Give this file, [CONTRIBUTING.md](CONTRIBUTING.md), and your exact task to your
agent before it edits code. Agents do not share private context automatically.
This guide, the code, tests, PRs, and human review are our shared contract. Ask
the task owner when a product or data decision is missing.

Yauwa is a WhatsApp voice bookkeeping bot for Nigerian retail traders with
limited literacy. Traders speak about shop stock and individual-unit sales;
spoken Nigerian Pidgin replies are essential. Gemini handles audio
transcription; Groq handles extraction and generated replies. ElevenLabs makes speech, Twilio carries WhatsApp,
and PostgreSQL stores ledger data. Read the current code before changing it:
parts of README.md still describe the original scaffold.

## Scope and ownership

- Work on a feature branch and change only the files needed for the assigned
  behavior. Teammate-owned TODOs and adjacent features are not invitations to
  implement them. Do not add restocking, accounting, or schema fields because
  they seem useful.
- Before changing a model, migration, extraction contract, or shared route,
  explain the proposed change, why it is needed, and how existing data and
  callers are affected. Wait for the task owner's decision on anything outside
  the assigned scope. Never invent defaults for missing business data.
- Follow existing patterns: use lazy `app.core.config.get_settings()`; use
  `.get_secret_value()` only when passing a secret to a client; use SQLModel
  `Session` and `app.db.session.get_engine()` / `get_session()`. Feature code
  owns commits and rollbacks. Do not create tables at import or health startup.
- Bound provider work with timeouts, handle errors and cleanup, and preserve
  Twilio signature validation. Replies should be friendly Nigerian Pidgin, with
  the existing text fallback if audio delivery fails.
- Do not log credentials, database URLs, raw audio, message bodies, or trader
  details in shared logs. Existing extraction prints are temporary debugging,
  not a production logging pattern.
- Keep `/health` working without provider keys or a database. Do not hardcode
  credentials, add undeclared packages, or commit directly to `main`.

## Each task, from start to PR

1. Read this guide, CONTRIBUTING.md, the assigned task, and the current
   implementation/tests. Tell the task owner what you found and which files
   you intend to edit. Raise unclear product decisions first.
2. Branch from current `main` (`feature/...` or `fix/...`). Check `git status`
   before editing and preserve changes you did not make.
3. Implement the smallest complete behavior. Add or update automated tests
   in the same PR. A function that only runs with live keys, without tests, is
   not a finished handoff. Mock Gemini, Groq, ElevenLabs, Twilio, and HTTP calls in
   unit tests; CI must not need secrets or spend provider credits.
4. Test success and failure paths that fit your feature: ambiguous extraction,
   multiple items or sales, missing data, repeated messages, provider timeouts,
   DB rollback, and safe user replies. Assert outputs and stored rows, not just
   that a function returned without raising.
5. In an activated Python 3.12 environment, run:

   ```sh
   python -m pip check
   ruff check .
   python -m unittest tests.providers.test_generation tests.services.test_onboarding tests.providers.test_tts tests.services.test_extraction tests.api.test_webhook tests.models.test_inventory_models
   ```

   Add your own test module to the command. Start `python -m uvicorn main:app
   --reload` and check `http://127.0.0.1:8000/health`. Current CI runs lint,
   webhook tests, and a credential-free health smoke check; it does not replace
   your feature tests or a live WhatsApp check.
6. Run the ngrok test below if you change a webhook, provider, reply, or DB
   write. Open a PR stating behavior, test results (and checks not run), live
   test notes, migration/environment changes or "none", and known limitations.
   One human approval is required before merge. An agent or CodeRabbit does
   not count as that approval.

## Local setup and credentials

Follow [README.md](README.md#local-setup) to create and activate a Python 3.12
virtual environment, install `requirements.txt`, and copy `.env.example` to
`.env`. Replace placeholders locally. Never paste secrets into an agent chat,
issue, PR, screenshot, or shared terminal log; never commit `.env`. Ask the
owner for approved development access or use your own development accounts.
Do not copy Railway production secrets or point local tests at production
PostgreSQL without explicit authorization.

| Variable | Where to get it / what to set |
| --- | --- |
| `DATABASE_URL` | An isolated local/dev PostgreSQL database and user; use synchronous `postgresql+psycopg2://...`. Create the database first. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | [Twilio Console](https://www.twilio.com/console), preferably a team-approved development account. Keep the auth token private. |
| `TWILIO_WHATSAPP_NUMBER` | [WhatsApp Sandbox](https://www.twilio.com/docs/whatsapp/sandbox) sender or approved test sender, formatted `whatsapp:+...`. |
| `TWILIO_WEBHOOK_URL` | Exact HTTPS ngrok URL ending in `/api/v1/webhook`; it must match Twilio's "When a message comes in" URL. |
| `PUBLIC_BASE_URL` | Same public HTTPS ngrok origin, without the webhook path; Twilio fetches reply MP3s here. |
| `GEMINI_API_KEY` | A development-project key from [Google AI Studio](https://ai.google.dev/gemini-api/docs/api-key). |
| `GEMINI_TRANSCRIPTION_MODEL` | Existing Gemini transcription model; preserve its configured value. |
| `GROQ_API_KEY` | A development key from the Groq console. |
| `GROQ_MODEL`, `GROQ_EXTRACTION_MODEL` | Defaults: `openai/gpt-oss-120b` for replies and `openai/gpt-oss-20b` for native JSON Schema extraction. |
| `ELEVENLABS_API_KEY` | Your [ElevenLabs API key](https://elevenlabs.io/docs/api-reference/authentication). |
| `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL_ID` | A voice available to your API plan and a compatible model. Preview Nigerian-accent/Pidgin speech and test via API; playground access alone does not prove API access. See [voice documentation](https://elevenlabs.io/docs/overview/capabilities/voices). |

Install the [ngrok agent](https://ngrok.com/download/linux) for your OS. Get
your own ngrok authtoken from its dashboard and run
`ngrok config add-authtoken YOUR_TOKEN` in your terminal once, replacing
`YOUR_TOKEN` with the private value. This token is not an app `.env` variable.
Provider quotas, voice access, and model names can change; check dashboards
and docs. Request missing access via the team's private channel. If you
cannot obtain it, finish mocked tests and explicitly mark live tests not run.

## End-to-end WhatsApp test through ngrok

Use a disposable development DB. Keep the app and tunnel open in separate
terminals:

1. Activate the virtual environment. Run `alembic upgrade head` against the
   development `DATABASE_URL` only. Check migration/model compatibility
   before relying on DB writes. Run `python -m uvicorn main:app --reload
   --port 8000`; `http://127.0.0.1:8000/health` should be healthy.
2. Run `ngrok http 8000`. If its HTTPS forwarding origin is
   `https://example.ngrok-free.app`, set these local `.env` values:

   ```dotenv
   PUBLIC_BASE_URL=https://example.ngrok-free.app
   TWILIO_WEBHOOK_URL=https://example.ngrok-free.app/api/v1/webhook
   ```

3. In [Twilio Sandbox configuration](https://www.twilio.com/docs/whatsapp/sandbox),
   set "When a message comes in" to that exact webhook URL with HTTP POST.
   Join the sandbox from your test WhatsApp number using the displayed join
   code, or use the team's approved test sender. `TWILIO_WHATSAPP_NUMBER`
   must be the sender this account is authorized to use.
4. Restart the app after changing `.env`: settings are cached. Send a short
   WhatsApp voice note with test-only details. Watch app logs, ngrok request
   inspection, and Twilio
   message status. HTTP 200 only means the webhook was accepted; background
   extraction, DB writes, and reply delivery may still fail. Confirm the
   spoken reply and inspect dev DB rows for persistence features.
5. Test the conversation: new trader states a name and then stock; bulk stock
   triggers a package-size question; the next note supplies units per package;
   a completed trader reports one or several unit sales. Also try an unclear
   or off-topic note. Check the Pidgin reply and that no incomplete or
   duplicate rows appear. Currently extraction prints stock/sales data but
   does not persist it. Pending bulk-stock follow-up is in memory and can
   disappear when the local app reloads or restarts.

The webhook is signature-validated: changed hostname, path, scheme, or
trailing slash can cause 403s. When ngrok assigns a new URL, update both local
`.env` values and Twilio, then restart the app. Leave the tunnel running until
Twilio fetches the reply MP3. A text fallback means audio failed; inspect the
earlier Gemini/ElevenLabs/media/Twilio error before calling the run successful.
Never put ngrok URLs in Railway production variables. Production migrations
and deployment require separate owner review.

## Next task: saving stock items

The next assigned task is to save extracted stock to `Item`, not to design
restocking or sales persistence. Read `app/services/extraction.py`,
`app/models/item.py`, `app/models/trader.py`, and Alembic revisions first.
The current extraction prints proposed rows with `item_name`,
`unit_quantity`, `bulk_type`, `bulk_quantity`, `unit_price=None`, and
`low_stock_threshold=None`. This is a debug preview, not an insert-ready
record. `Item.low_stock_threshold` is required, and the initial item
migration has an older column layout. Agree with the owner how thresholds are
obtained and how schema/data migrate before any write. Do not invent a
threshold or silently rewrite an existing migration. Confirm when direct-unit
and bulk stock are saved, and what should happen for an existing item.

DB writes need tests. Use a disposable PostgreSQL database with `alembic
upgrade head` for integration tests, plus mocked-provider tests around the
flow. Test one item and multiple items, correct trader ID, package arithmetic
(4 bags x 20 = 80 units), direct-unit stock, unknown package sizes, repeated
webhook delivery, rollback on DB error, and read-back after commit. A bad item
must not leave half a batch saved. Never infer package sizes or persist
uncertain extraction as fact. Decide overwrite/merge and idempotency policy
with the owner; the current in-process duplicate filter does not survive
restarts. Keep the spoken follow-up, and change "I never save am yet" only
after the save genuinely commits. After automated tests, repeat the full
two-note stock flow via ngrok and inspect PostgreSQL rows. Mock-only tests do
not prove the live voice path; one live run does not replace repeatable DB
tests.

## Prompt to hand to an agent

> Read GUIDE.md, CONTRIBUTING.md, and my assigned task first. Inspect current
> code and tests, then tell me the files you intend to edit and any product or
> schema decisions needing approval. Implement only the agreed scope. Add
> success and failure tests, run Ruff and relevant tests, and help me run a
> local ngrok/WhatsApp check when the voice flow changes. Do not use production
> credentials or DB data. Show exact verification results and prepare a PR
> handoff; do not merge without human review.
