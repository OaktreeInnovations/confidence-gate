# Contributing to Confidence Gate

## Local setup

**Prerequisites:** Docker, Docker Compose, Python 3.11, Node 20

```bash
git clone https://github.com/OaktreeInnovations/confidence-gate.git
cd confidence-gate
cp .env.example .env  # fill in credentials
make up
make health
```

## Backend tests

```bash
cd backend
pip install -r requirements.txt
pytest tests/ --tb=short -q
```

Tests use in-memory mocks — no real MongoDB, Redis, or AI calls required.

## Frontend checks

```bash
cd frontend
npm install
npm run lint
npm run build
```

## Adding an AI provider

1. Create `backend/app/ai/providers/<name>.py` implementing `ChatProvider`:

```python
from app.ai.base import Message

class MyProvider:
    def complete(self, messages: list[Message], *, max_tokens=600, temperature=0.1, json_mode=False) -> str:
        ...
```

2. Register it in `backend/app/worker/tasks/compute_release_report.py` inside `_build_chat_provider`.

3. Add the new provider value to `AI_PROVIDER` docs in `.env.example` and `README.md`.

4. Update `CONTRIBUTING.md` (this file).

## Adding an auth provider

1. Create `backend/app/auth/providers/<name>.py` implementing `AuthProvider`:

```python
class MyAuthProvider:
    async def verify_id_token(self, token: str) -> dict:
        # must return a dict with at least {"uid": str}
        ...
```

2. Wire it in `backend/app/main.py` inside the `lifespan` function.

3. Update `.env.example` and `README.md`.

## PR conventions

- One logical change per PR
- Tests added or updated for new behaviour
- `make health` passes locally
- No credentials or secrets committed

## Reporting issues

Use the GitHub issue templates — bug reports and feature requests have structured forms that help us triage faster.
