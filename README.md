# Yauwa

Yauwa is a WhatsApp bookkeeping assistant for unlettered Nigerian traders. A trader can send a voice note about stock or sales and receive a spoken reply in Nigerian Pidgin. The application also helps track low stock, prepare a market list, and record restocking.

## Judge walkthrough on WhatsApp

Use the WhatsApp number supplied with the submission. Use a
fresh WhatsApp number for the first run because Yauwa remembers onboarded traders.

1. Send a **voice note** saying your name, for example, “My name is Amina.”
   Expect a spoken welcome and a prompt to describe shop stock.
2. Send a voice note such as “I have 30 biscuits in my shop.” Expect Yauwa to
   acknowledge and save the stock.
3. Send “I sold 3 biscuits for 100 naira each.” Expect a sales confirmation; the
   recorded biscuit quantity should decrease from 30 to 27.
4. Ask for a market list or report a restock to try those flows. If you describe
   bulk stock without saying how many pieces are in each pack, Yauwa asks for
   that detail in a follow-up voice note.

Replies arrive after background processing, so allow time for the audio message.
Use test-only details. Text messages can reach the bot, but the bookkeeping flow
is designed for voice notes.

## What works

- Voice onboarding records a trader's name before bookkeeping starts.
- Voice notes can add stock, record one or more sales, request a market list, and confirm restocking.
- Bulk stock without a stated pack size prompts a follow-up voice note.
- Recorded sales update matching inventory and can trigger low-stock alerts.
- Replies use ElevenLabs audio when available, with a text fallback if audio delivery fails.

Twilio sends WhatsApp webhooks to FastAPI. Gemini transcribes voice notes; Groq extracts structured bookkeeping details. Services validate and save the result through SQLModel and PostgreSQL. ElevenLabs produces reply audio, which Twilio retrieves from a temporary media endpoint.

```text
WhatsApp → Twilio webhook → Gemini transcription → Groq extraction
         → stock/sales services → PostgreSQL
         → Pidgin reply → ElevenLabs audio → Twilio → trader
```

The webhook validates Twilio's signature and acknowledges accepted messages before processing them in the background. HTTP 200 confirms receipt, not completion of the ledger write or reply.

## Repository layout

```text
main.py                  FastAPI app
app/
  api/                   Health, webhook, and temporary media routes
  core/config.py         Lazy environment settings
  schemas/               Webhook and extraction data contracts
  services/              Onboarding, message processing, stock, sales, market
  providers/             Gemini/Groq, ElevenLabs, and Twilio integrations
  db/                    Database sessions and ledger writes
  models/                SQLModel entities
migrations/              Alembic database revisions
tests/                   API, service, provider, model, DB, and integration tests
```

The public routes are `/health`, `/api/v1/webhook`, and `/api/v1/media/{token}`. Interactive API documentation is available at `/docs`.

## Run locally

You need Python 3.12, PostgreSQL, and development credentials for Twilio, Gemini, Groq, and ElevenLabs to exercise the complete voice flow. The health route and offline tests do not need provider credentials or a running database.

From the repository root:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` and copy the template with `Copy-Item .env.example .env`.

Replace the placeholders in `.env` with development values. Keep that file private. These settings are required for the full application flow:

| Setting | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL URL, such as `postgresql+psycopg2://USER:PASSWORD@localhost:5432/yauwa` |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Twilio account credentials |
| `TWILIO_WHATSAPP_NUMBER` | Authorized WhatsApp sender, including the `whatsapp:` prefix |
| `TWILIO_WEBHOOK_URL` | Exact public HTTPS webhook URL ending in `/api/v1/webhook` |
| `PUBLIC_BASE_URL` | Public HTTPS origin for reply audio; must match the webhook's origin |
| `GEMINI_API_KEY`, `GEMINI_TRANSCRIPTION_MODEL` | Gemini credentials and a configured transcription model |
| `GROQ_API_KEY` | Groq credential for structured extraction and generated replies |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | ElevenLabs credentials for spoken replies |

`GROQ_MODEL`, `GROQ_EXTRACTION_MODEL`, and `ELEVENLABS_MODEL_ID` have defaults in `app/core/config.py`. `GEMINI_TRANSCRIPTION_MODEL` does **not** have a default. Set `TEST_DATABASE_URL` only when running the PostgreSQL integration test.

Create the development database, then apply the checked-in Alembic migrations:

```sh
python -m alembic upgrade head
python -m uvicorn main:app --reload
```

Open <http://127.0.0.1:8000/health>. It should return `{"status":"healthy"}`. App startup and the health route do not connect to PostgreSQL; bookkeeping does.

## Connect WhatsApp to the local app

These steps use [ngrok](https://ngrok.com/download) to give Twilio a public HTTPS
address for the app running on your computer. Keep PostgreSQL running and apply
the migrations in the setup section first.

1. Start Yauwa in one terminal:

   ```sh
   python -m uvicorn main:app --reload --port 8000
   ```

2. [Install ngrok](https://ngrok.com/download). Once, replace the placeholder
   with your own ngrok authtoken and run:

   ```sh
   ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN
   ```

   Keep the authtoken out of this repository's `.env`. In a second terminal,
   start a tunnel to the app's port:

   ```sh
   ngrok http 8000
   ```

   Copy the HTTPS forwarding URL that ngrok displays. For example, if it shows
   `https://abc123.ngrok-free.app`, check that
   `https://abc123.ngrok-free.app/health` returns `{"status":"healthy"}`.

3. Set these values in the repository-root `.env` using **your** forwarding URL:

   ```dotenv
   PUBLIC_BASE_URL=https://abc123.ngrok-free.app
   TWILIO_WEBHOOK_URL=https://abc123.ngrok-free.app/api/v1/webhook
   ```

   Set `TWILIO_WHATSAPP_NUMBER` to the WhatsApp sender shown in your Twilio
   account, with the `whatsapp:` prefix. Restart Uvicorn after changing `.env`;
   settings are cached. Keep both the app and ngrok running.

4. In Twilio, configure incoming WhatsApp messages to send an **HTTP POST** to
   the exact `TWILIO_WEBHOOK_URL` above, then save:

   - For the [Twilio Sandbox](https://www.twilio.com/docs/whatsapp/sandbox),
     open its legacy Console **Sandbox settings** and set
     **When a Message Comes in**.
   - For a trial account using
     [Try out WhatsApp](https://help.twilio.com/articles/55716571409819-How-do-I-receive-WhatsApp-messages-while-testing),
     open **Receive a message**, choose **Custom**, and set its **Webhook URL**.
     For an approved WhatsApp sender, set its inbound webhook in that sender's
     configuration.

5. If using a Sandbox or trial testing environment, connect the tester's
   WhatsApp account first: scan Twilio's QR code or send its displayed
   `join <sandbox code>` message to the displayed number. Wait for Twilio's
   confirmation, then send a **voice note** from that same phone and follow
   the judge walkthrough above.

You should see a POST to `/api/v1/webhook` in the ngrok request log, followed by
a WhatsApp reply after background processing. Check application logs and the
development database when verifying a saved sale or stock item. The HTTP 200
webhook response only confirms receipt. If ngrok gives you a new URL, update
both `.env` values and Twilio's webhook URL, then restart the app. The URLs
must match exactly for Twilio signature validation; keep ngrok running until
Twilio has fetched the reply audio.

## Checks

Run the checks used by GitHub Actions before opening a pull request:

```sh
python -m pip check
ruff check .
python -m unittest \
  tests.api.test_webhook \
  tests.services.test_extraction \
  tests.services.test_market_delivery \
  tests.services.test_market_restock \
  tests.providers.test_generation \
  tests.providers.test_groq \
  tests.services.test_onboarding \
  tests.providers.test_tts \
  tests.models.test_inventory_models \
  tests.db.test_ledger \
  tests.db.test_sales_flow
```

CI also starts Uvicorn and checks `/health`. The tests above mock external providers and need no live credentials. To run the separate PostgreSQL inventory integration test, set `TEST_DATABASE_URL` to a disposable migrated database and run:

```sh
python -m unittest tests.integration.test_save_stock_items
```

Some tests deliberately log simulated provider failures while checking fallback behavior; the test command's final `OK` or `FAILED` line determines its result.

## Deployment and current limits

The app can run with `uvicorn main:app --host 0.0.0.0 --port $PORT`. Apply Alembic migrations to the deployment database before handling trader messages, configure the same environment variables as above, and set `/health` as the health-check path. CI checks code and startup; it does not deploy or migrate a database.

Duplicate webhook tracking and pending bulk-stock follow-ups live in process memory. They can be lost on restart and are not shared across workers. Background processing also means webhook acknowledgement is not a delivery guarantee. The offline suite covers application behavior with mocked providers; a live WhatsApp and PostgreSQL run is a separate verification step. Some current debug prints include extracted test content, so use test-only data for local trials.
