# Instructions for every AI coding agent

Yauwa is a WhatsApp voice bookkeeping bot for Nigerian informal traders with
limited or no literacy. It will log sales, track stock, and manage restocking.
Voice input and spoken Nigerian Pidgin replies are essential: literacy is the
core constraint, not a nice-to-have. Gemini handles transcription, extraction,
and reply generation; ElevenLabs handles TTS only; Twilio handles WhatsApp.

## CRITICAL: respect task ownership and intentional stubs

**Never fill in stubbed files under app/models/, app/schemas/, or app/ai/ unless the task you were specifically given is to implement that exact file. These are intentionally left as TODO placeholders for other teammates to build during the hackathon build week. If your assigned task doesn't mention a file by name, leave it untouched — even if it looks incomplete or you think you could 'helpfully' finish it. Completing someone else's stub causes merge conflicts and steps on a task that isn't yours.**

## Always do this

- Read this guide, CONTRIBUTING.md, and the assigned task before editing.
- Follow the existing file structure. Do not invent new top-level folders
  without discussing them with the teammate assigning the task.
- Keep functions small and commented where intent needs explanation. Teammates
  are learning from this code, not just running it.
- Match existing patterns instead of introducing a different style.
- Never hardcode API keys. Import `get_settings` from `app.config`; configuration
  reads environment variables and `.env`. No other application module should
  use `os.environ`, `os.getenv`, or load dotenv directly.
- Call `get_settings()` when configuration is needed, not at module import time.
  Secret fields use `.get_secret_value()` when passed to a client. Do not log
  settings, credentials, or database URLs.
- Reuse the synchronous `get_engine()` / `get_session()` database infrastructure.
  Sessions close automatically; feature code owns commits. Do not create tables
  on import or health-check startup.
- Keep `/health` able to start without external credentials or a database.
- Explain changes and verification in the PR; run Ruff and relevant task checks.

## Never do this

- Do not modify the database schema without flagging the change to the team first.
- Do not remove or bypass the **Pidgin-only reply language** rule.
- Do not add dependencies without checking `requirements.txt` first. Reuse an
  existing dependency when possible; explain necessary additions in the PR and
  update the shared requirements rather than installing them only locally.
- Do not commit directly to `main`; use the documented branch naming and PR flow.
- Do not expand the task into unassigned models, schemas, AI logic, or routes.

Every PR still needs **human review before merging**, regardless of an agent's
confidence in its output or CodeRabbit's review. Report checks honestly; never
claim branch protection or deployment is active just because config/docs exist.

## Prompt teammates can paste into any coding agent

> Read GUIDE.md and CONTRIBUTING.md before starting this task. Follow existing
> patterns and implement only my assigned scope. Leave all model, schema, and AI
> stubs untouched unless I name that exact file for implementation. Explain your
> changes and verification, and prepare the work for human PR review.
