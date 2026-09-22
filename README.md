# Yauwa

Yauwa is a voice-first WhatsApp bookkeeping bot for Nigerian informal traders
who cannot read or write, or have limited literacy. Traders will log sales,
track stock, and manage restocking by speaking. Replies must be in Nigerian
Pidgin and delivered as audio: literacy is the core constraint.

**Current status:** the signed Twilio webhook accepts text messages and downloads
the first audio attachment to a temporary file. Models, AI integrations, and
ledger business logic are separate tasks for our four-person, six-day build
week. Their TODO files are intentional.

## Planned architecture

FastAPI will receive WhatsApp voice notes through Twilio webhooks. Gemini will
transcribe the audio and extract the trader's intent and bookkeeping details.
SQLModel will persist the ledger in PostgreSQL. Gemini will generate a
Pidgin-only response, ElevenLabs will turn that response into speech, and Twilio
will deliver the audio to the trader. ElevenLabs handles TTS only; this pipeline
is a design overview, not implemented behavior.

Voice note → Twilio webhook → Gemini transcription/extraction → Postgres ledger
update → Gemini reply generation, Pidgin only → ElevenLabs TTS → sent back via Twilio

## Tech stack and layout

- Python 3.12 and FastAPI: HTTP application.
- PostgreSQL, SQLModel, and psycopg2-binary: synchronous database access.
- pydantic-settings: centralized environment configuration.
- Gemini (`google-genai`): transcription, extraction, and reply generation.
- ElevenLabs: TTS only; Twilio: WhatsApp transport. HTTPX is available for HTTP calls.
- Ruff and GitHub Actions: lint and startup checks; Render: deployment.

```text
main.py                 Existing FastAPI app and /health endpoint
app/
  routes/routes.py      Existing /api/v1 router
  routes/webhook.py     Signed /webhook receiver and audio downloader
  models/              Trader, Item, Sale placeholders
  schemas/webhook.py   Incoming Twilio payload and response schemas
  ai/                  Extraction, reply generation, and TTS placeholders
  config.py            Settings and cached get_settings()
  db/session.py        Cached get_engine() and yielding get_session()
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before your first branch and ask your
coding agent to read [GUIDE.md](GUIDE.md) before every task.

## Local setup

1. Install Git and Python **3.12**. Get the team's repository URL, then replace
   `REPOSITORY_URL` below with that URL:

   ```sh
   git clone REPOSITORY_URL yauwa
   cd yauwa
   ```

2. Create a virtual environment. It keeps this project's packages separate from
   other Python projects. On Linux/macOS:

   ```sh
   python3.12 -m venv .venv
   source .venv/bin/activate
   ```

   On Windows PowerShell:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   Activate it again in each new terminal. If you already use `fastapi-env`,
   activate that environment instead; you do not need to recreate it.

3. Install the shared dependencies from the repository root:

   ```sh
   python -m pip install -r requirements.txt
   python -m pip check
   ```

   Direct dependency versions are pinned to keep teammates aligned. This is not
   a full transitive lockfile. Ruff is included so local and CI lint use the same version.

4. Copy the environment template. On Linux/macOS:

   ```sh
   cp .env.example .env
   ```

   On Windows PowerShell:

   ```powershell
   Copy-Item .env.example .env
   ```

   Edit `.env` locally. All `XXXXXX` entries are placeholders, not credentials.
   Use your own development accounts and free-tier/trial access where available;
   check each provider's current limits and eligibility. Never commit `.env`.

   | Variable | What to supply |
   | --- | --- |
   | `DATABASE_URL` | A PostgreSQL connection URL for your own development database |
   | `TWILIO_ACCOUNT_SID` | Your Twilio account SID |
   | `TWILIO_AUTH_TOKEN` | Your Twilio auth token |
   | `TWILIO_WHATSAPP_NUMBER` | Your Twilio sender, with `whatsapp:` prefix and international number |
   | `TWILIO_WEBHOOK_URL` | Exact public HTTPS URL Twilio calls, ending in `/webhook` |
   | `GEMINI_API_KEY` | Your Google AI Studio API key |
   | `ELEVENLABS_API_KEY` | Your ElevenLabs API key |
   | `ELEVENLABS_VOICE_ID` | The voice ID selected for your development account |

   Provider setup: [Gemini](https://ai.google.dev/gemini-api/docs/get-started),
   [Twilio WhatsApp Sandbox](https://www.twilio.com/docs/whatsapp/sandbox),
   [ElevenLabs](https://elevenlabs.io/docs/overview/quickstart).

   For database tasks, install PostgreSQL locally or provision a development
   PostgreSQL database and create a database/user. A local URL has this shape:
   `postgresql+psycopg2://USER:PASSWORD@localhost:5432/yauwa`.
   Replace the sample values, URL-encode special characters in credentials, and
   retain any SSL parameters supplied by a hosted provider. Use the synchronous
   `postgresql://` or `postgresql+psycopg2://` URL format, not an async driver URL.
   No tables or migrations are created by this scaffold.

5. Start the app from the repository root:

   ```sh
   fastapi dev main.py
   ```

   Alternatively:

   ```sh
   uvicorn main:app --reload
   ```

   Visit <http://127.0.0.1:8000/health>; expect `{"status":"healthy"}`.
   API documentation is at <http://127.0.0.1:8000/docs>.
   Stop the server with Ctrl+C.

The health endpoint needs no credentials or running database. Settings validate
only when `get_settings()` is called; at that point its seven values are required,
and `XXXXXX` is not a valid database URL. Placeholder provider keys cannot make
real API calls. Settings read the repository-root `.env`; process environment
variables take precedence. Restart after changing configuration because settings
and the engine are cached.

Future code must use `get_settings()` instead of reading the environment
directly. Secret fields expose their value with `.get_secret_value()` only when
passing it to a provider client; do not log settings or database URLs.
Database routes can use `Depends(get_session)`. Sessions close automatically;
feature code owns commits. Engine construction does not connect or create tables.

The webhook uses the focused `get_twilio_settings()` accessor, so it requires
only `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_WEBHOOK_URL`.

## Twilio webhook setup and local test

Expose the local server through an HTTPS tunnel, then set `TWILIO_WEBHOOK_URL`
to the exact public URL, such as `https://your-tunnel.example/webhook`. In the
Twilio WhatsApp Sandbox or sender settings, configure **When a message comes in**
to use the same URL with HTTP POST. Twilio signs the URL, so changes to its
scheme, hostname, path, query string, or trailing slash will invalidate requests.

For a signed local text-message test, keep the app running and use another
terminal. The example signs every field that it sends:

```sh
export TWILIO_WEBHOOK_URL='https://your-tunnel.example/webhook'
export TEST_SENDER='whatsapp:+2348012345678'
export TWILIO_SIGNATURE=$(python - <<'PY'
from app.config import get_twilio_settings
from twilio.request_validator import RequestValidator

settings = get_twilio_settings()
fields = {"From": "whatsapp:+2348012345678"}
validator = RequestValidator(settings.twilio_auth_token.get_secret_value())
print(validator.compute_signature(str(settings.twilio_webhook_url), fields))
PY
)
curl -X POST "${TWILIO_WEBHOOK_URL}" \
  -H "X-Twilio-Signature: ${TWILIO_SIGNATURE}" \
  --data-urlencode "From=${TEST_SENDER}"
```

A text-only request returns `file_path: null`. An audio request returns an
absolute temporary path on the server. This JSON response is for development;
it does not yet send a TwiML or WhatsApp reply. Successful files remain available
for the future AI extraction step, which must delete each file after consuming
it. Failed and partial downloads are removed immediately.

## Checks and review

Run `ruff check .` before opening a PR. GitHub Actions runs `lint` and `smoke`
on every push and pull request. The smoke job imports infrastructure, starts the
app, and checks `/health` without credentials or a database. Mypy and a full
pytest suite are deliberately deferred for the build week.

**Required main-branch policy:** every merge needs a PR, at least one human
approval, and passing `lint` and `smoke` checks; direct pushes are prohibited.
GitHub must enforce this through branch protection, including administrators.
Files in this repository cannot activate those settings. Activation has not been
verified here; follow the administrator checklist in [CONTRIBUTING.md](CONTRIBUTING.md).

For automated first-pass review, an administrator should
[install CodeRabbit](https://docs.coderabbit.ai/getting-started/quickstart) for
this GitHub repository and enable automatic reviews of PRs targeting `main` in
its repository settings. Verify it reviews a test PR. This external setup is not
performed by these files. CodeRabbit supplements, and never replaces, the
required human approval.

## Deployment setup for the repository owner

Connect a Render web service to this GitHub repository and select branch `main`:

- Runtime: Python 3.12 (set Render's `PYTHON_VERSION` to a supported 3.12 patch).
- Build command: `pip install -r requirements.txt`.
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`.
- Health check path: `/health`.
- Auto-deploy: **After CI Checks Pass**.
- Set the eight application variables in Render's environment settings using
  deployment credentials and the deployment PostgreSQL URL; do not upload `.env`.

Render's [native GitHub integration](https://render.com/docs/deploys) handles
deployment after changes merge to `main` and checks pass. GitHub Actions is the
merge gate, not the deployment runner. Render setup is an administrator step
and has not been activated by this scaffold.
