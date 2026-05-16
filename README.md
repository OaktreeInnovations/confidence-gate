# Confidence Gate

[![Backend CI](https://github.com/OaktreeInnovations/confidence-gate/actions/workflows/backend-ci.yml/badge.svg)](https://github.com/OaktreeInnovations/confidence-gate/actions/workflows/backend-ci.yml)
[![Frontend CI](https://github.com/OaktreeInnovations/confidence-gate/actions/workflows/frontend-ci.yml/badge.svg)](https://github.com/OaktreeInnovations/confidence-gate/actions/workflows/frontend-ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

AI-powered release gating platform. Run your test suite, get a confidence score, decide whether to ship.

## What it does

- Executes test cases via Playwright and assigns each run a **confidence score** (0–100)
- Detects flaky tests, selector drift, and hidden failure patterns across runs
- Optionally compares coverage against a PRD using an AI model
- Issues a gate decision: **ship** / **caution** / **block**

## Quick start

```bash
# 1. Clone
git clone https://github.com/OaktreeInnovations/confidence-gate.git
cd confidence-gate

# 2. Configure
cp .env.example .env
# Edit .env — set credentials and choose your auth/AI provider

# 3. Start
make up

# 4. Check
make health
```

Services once running:

| Service        | URL                        |
|---------------|---------------------------|
| Backend API   | http://localhost:8001      |
| Frontend      | http://localhost:3001      |
| MinIO Console | http://localhost:9005      |

## Auth providers

Set `AUTH_PROVIDER` in `.env`:

| Value      | Description                                          |
|-----------|------------------------------------------------------|
| `firebase` | Firebase Auth (default). Requires `FIREBASE_PROJECT_ID` and a `backend/firebase-service-account.json`. |
| `local`    | HS256 JWT — no Firebase required. For local dev only. Set `LOCAL_AUTH_SECRET`. |

## AI providers

Set `AI_PROVIDER` in `.env`:

| Value       | Env vars needed                   |
|------------|-----------------------------------|
| `openai`   | `OPENAI_API_KEY`                  |
| `anthropic`| `ANTHROPIC_API_KEY`               |
| `ollama`   | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` |

The AI layer is optional. If no key is configured the platform still works — the AI risk adjustment step is skipped and confidence scoring uses deterministic signals only.

## Stack

**Backend:** Python 3.11 · FastAPI · Celery · MongoDB · Redis · MinIO · Playwright

**Frontend:** Next.js 15 · React 19 · TypeScript · Tailwind CSS v4

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions, how to run tests, and how to add a new AI or auth provider.

## License

MIT — see [LICENSE](LICENSE).
