# Confidence Gate

[![Backend CI](https://github.com/OaktreeInnovations/confidence-gate/actions/workflows/backend-ci.yml/badge.svg)](https://github.com/OaktreeInnovations/confidence-gate/actions/workflows/backend-ci.yml)
[![Frontend CI](https://github.com/OaktreeInnovations/confidence-gate/actions/workflows/frontend-ci.yml/badge.svg)](https://github.com/OaktreeInnovations/confidence-gate/actions/workflows/frontend-ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**AI-powered release gating platform.** Write test cases in plain English, let Confidence Gate execute them with Playwright, score the results, and decide whether to ship.

---

## How it works

1. **Write test cases in plain English** — no code, no selectors. Describe what a user does and what should happen.
2. **Trigger a test run** — Confidence Gate uses an AI executor to drive a real browser through each step, taking screenshots along the way.
3. **Get a confidence score (0–100)** — calculated from pass rates, flaky history, selector stability, and optional AI risk analysis against your PRD.
4. **Gate your release** — the score maps to a `ship` / `caution` / `block` decision you can act on in CI or the dashboard.

Behind the scenes the AI executor:
- Resolves elements from natural-language descriptions using an accessibility tree + selector engine
- Detects DOM mutations, URL changes, and visual state to verify each step
- Self-heals broken selectors with an AI repair loop
- Caches successful intents so repeat runs are faster and cheaper

---

## Features

| Feature | Description |
|---|---|
| Natural-language test cases | Write steps like "click the login button" — no XPath or CSS selectors |
| AI step execution | Playwright browser driven by GPT-4o vision + DOM understanding |
| Selector self-healing | Broken selectors are repaired automatically between runs |
| Flaky test detection | Statistical analysis flags tests with inconsistent pass/fail history |
| Confidence scoring | Deterministic + AI-adjusted score per test run and release |
| Release validation | Gate a release against a full test suite with a single API call |
| PRD coverage check | Optionally compare test coverage against a product requirements document |
| Failure graph | Visualises which tests fail together to surface hidden dependencies |
| Model drift detection | Alerts when AI verification confidence degrades over time |
| Nightly learning | Scheduled jobs recalibrate thresholds and global weights from outcome history |
| Screenshot evidence | Every step stores a screenshot in MinIO for post-run review |
| Webhooks | POST run results to any endpoint on completion |
| Multi-org | Full org/project isolation with per-user membership |

---

## Quick start

**Prerequisites:** Docker and Docker Compose.

```bash
# 1. Clone
git clone https://github.com/OaktreeInnovations/confidence-gate.git
cd confidence-gate

# 2. Configure
cp .env.example .env
# Edit .env — minimum required: auth credentials and at least one AI provider key

# 3. Start everything
make up

# 4. Verify all services are healthy
make health
```

Open **http://localhost:3001** to access the dashboard.

### Services

| Service | URL | Notes |
|---|---|---|
| Frontend | http://localhost:3001 | Next.js dashboard |
| Backend API | http://localhost:8001 | FastAPI — docs at `/docs` |
| MinIO Console | http://localhost:9005 | Screenshot storage |
| MongoDB | localhost:27019 | Direct access for debugging |
| Redis | localhost:6381 | Celery broker |

### Makefile commands

| Command | Description |
|---|---|
| `make up` | Build and start all services |
| `make down` | Stop services (preserves volumes) |
| `make down-all` | Stop and delete all volumes |
| `make logs` | Tail all container logs |
| `make health` | Hit the health endpoints |
| `make ps` | Show container status |
| `make build` | Rebuild images without starting |

---

## Configuration

All configuration is via environment variables in `.env`. Copy `.env.example` to get started.

### Auth providers

Set `AUTH_PROVIDER`:

| Value | Description |
|---|---|
| `firebase` | Firebase Authentication (default). Requires `FIREBASE_PROJECT_ID` and `backend/firebase-service-account.json`. |
| `local` | HS256 JWT — no Firebase required. For local development only. Set `LOCAL_AUTH_SECRET`. |

**Firebase setup:**
1. Create a Firebase project and enable Email/Password authentication
2. Download a service account key from Project Settings → Service Accounts
3. Save it as `backend/firebase-service-account.json`
4. Set `FIREBASE_PROJECT_ID`, `NEXT_PUBLIC_FIREBASE_API_KEY`, `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN`, `NEXT_PUBLIC_FIREBASE_PROJECT_ID`

**Local auth setup:**
```env
AUTH_PROVIDER=local
LOCAL_AUTH_SECRET=your-secret-key-at-least-32-chars
```
The backend issues signed JWTs directly — no external service needed.

### AI providers

Set `AI_PROVIDER`:

| Value | Required env vars | Notes |
|---|---|---|
| `openai` | `OPENAI_API_KEY` | Default. Uses GPT-4o for execution and vision. |
| `anthropic` | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Claude models. Default model: `claude-3-5-haiku-20241022`. |
| `ollama` | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | Local models via Ollama. Vision steps may degrade without a multimodal model. |

The AI layer is **optional for basic use**. Without an API key the platform still executes tests and scores them — the AI risk-adjustment step is skipped and scoring uses deterministic signals only.

### Full `.env` reference

```env
# MongoDB
MONGO_INITDB_ROOT_USERNAME=admin
MONGO_INITDB_ROOT_PASSWORD=changeme

# MinIO
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=changeme
MINIO_DEFAULT_BUCKET=confidence-gate-artifacts

# Auth
AUTH_PROVIDER=firebase                        # or "local"
FIREBASE_PROJECT_ID=your-project-id          # firebase only
LOCAL_AUTH_SECRET=change-me                  # local only

# AI
AI_PROVIDER=openai                           # or "anthropic" / "ollama"
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-3-5-haiku-20241022
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2

# Frontend
NEXT_PUBLIC_FIREBASE_API_KEY=               # firebase only
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=          # firebase only
NEXT_PUBLIC_FIREBASE_PROJECT_ID=           # firebase only
NEXT_PUBLIC_API_URL=http://localhost:8001
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (localhost:3001)                                    │
│  Next.js 15 · React 19 · TypeScript · Tailwind CSS v4       │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP
┌───────────────────────────▼─────────────────────────────────┐
│  Backend API (localhost:8001)                                │
│  FastAPI · Python 3.11                                       │
│  Auth: FirebaseAuthProvider | LocalAuthProvider              │
└──────┬────────────────────┬────────────────────────────────┘
       │ Celery tasks        │ Queries
┌──────▼──────────┐  ┌──────▼──────┐  ┌───────────────────┐
│  Workers (×4)   │  │   MongoDB   │  │  MinIO            │
│  Celery + Redis │  │  (data)     │  │  (screenshots)    │
│  Playwright     │  └─────────────┘  └───────────────────┘
│  AI executor    │
└─────────────────┘
       │
┌──────▼──────────────────────────────────────────────────────┐
│  AI Provider                                                 │
│  OpenAI GPT-4o | Anthropic Claude | Ollama (local)          │
└─────────────────────────────────────────────────────────────┘
```

### Background jobs (Celery Beat)

| Job | Schedule | Description |
|---|---|---|
| `check_validation_sla` | Every 5 min | Marks stalled runs as timed out |
| `prune_expired_evidence` | Daily | Removes old screenshot evidence |
| `compute_benchmarks` | Daily | Refreshes project-level pass rate benchmarks |
| `detect_flaky_degradation` | Daily | Flags tests whose flakiness rate is increasing |
| `detect_model_drift` | Weekly | Alerts when AI confidence scores degrade |
| `nightly_learning_chain` | Nightly | Syncs outcomes → recalibrates thresholds → optimises global weights |

---

## REST API

The API is documented interactively at **http://localhost:8001/docs** once the backend is running.

Key endpoints:

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/ready` | Readiness check (DB + Redis) |
| `POST` | `/api/auth/register` | Create user account |
| `GET` | `/api/auth/me` | Current user + org |
| `POST` | `/api/orgs` | Create organisation |
| `GET/POST` | `/api/projects` | List / create projects |
| `GET/POST` | `/api/test-cases` | List / create test cases |
| `POST` | `/api/test-runs` | Trigger a test run |
| `GET` | `/api/test-runs/{id}` | Get run results + step evidence |
| `POST` | `/api/release-validations` | Gate a release |
| `GET` | `/api/intelligence/risk` | AI risk analysis for a release |

---

## Writing test cases

Test cases are plain-English step lists. Each step has an **action** (what to do) and an **expected** (what should be true afterwards).

**Tips for reliable steps:**

- **Navigate with exact URLs.** Include the full path, not just the domain.
- **Describe outcomes, not mechanics.** Instead of *"click the button with class btn-primary"*, write *"click the Login button"*.
- **Keep expected results visual and observable.** The AI verifies steps from a screenshot — describe what you can see, not what the code does.
- **Use test data for credentials.** Put emails and passwords in the `test_data` field and reference them in steps as *"the email from the test data"*.
- **One action per step.** Compound steps (*"enter email and password then click submit"*) are harder to verify and debug.

**Example test case:**

```json
{
  "title": "User login",
  "test_type": "ui",
  "test_data": { "email": "user@example.com", "password": "secret" },
  "steps": [
    {
      "step_number": 1,
      "action": "go to https://your-app.com/login",
      "expected": "a login form with email and password fields is visible"
    },
    {
      "step_number": 2,
      "action": "enter the email from the test data in the email field",
      "expected": "the email field contains the entered email address"
    },
    {
      "step_number": 3,
      "action": "enter the password from the test data in the password field",
      "expected": "the password field is filled"
    },
    {
      "step_number": 4,
      "action": "click the Sign In button",
      "expected": "the dashboard is displayed and the login form is no longer visible"
    }
  ]
}
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for:
- Local development setup
- Running backend tests and frontend lint
- How to add a new AI provider
- How to add a new auth provider
- PR conventions

---

## License

MIT — see [LICENSE](LICENSE).
