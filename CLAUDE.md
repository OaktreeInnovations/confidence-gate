# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Confidence Gate is an AI Autonomous Test Execution Platform. It uses Playwright for browser automation, OpenAI for AI-driven test decision making, and produces confidence scores and flakiness analysis across test runs.

## Local Development

All services run via Docker Compose:

```bash
make up       # Start all services (builds images first)
make down     # Stop all services and remove volumes
make logs     # Tail all service logs
make health   # Check health of backend and frontend
make ps       # Show running container status
make build    # Rebuild all Docker images
```

Services once running:
- Backend API: http://localhost:8001
- Frontend: http://localhost:3001
- MinIO Console: http://localhost:9005
- MongoDB: localhost:27019
- Redis: localhost:6381

Requires a `.env` file with credentials and a `firebase-service-account.json` in the backend directory.

## Backend

**Stack:** Python 3.11, FastAPI, Celery, Motor (async MongoDB), Redis, MinIO, Firebase Admin, OpenAI, Playwright

**Run tests:**
```bash
cd backend
pip install -r requirements.txt
pytest tests/                          # All tests
pytest tests/worker/test_behavior_detection.py  # Single file
pytest tests/ -k "test_name"           # Single test by name
```

**Start dev server (outside Docker):**
```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**Start Celery worker (outside Docker):**
```bash
cd backend
celery -A app.worker.celery_app worker -Q qualora.default --loglevel=info
```

### Backend Architecture

```
backend/app/
├── api/              # Route handlers (auth, orgs, projects, test_cases, test_runs, intelligence, release_validations)
├── models/           # Pydantic data models
├── clients/          # External service wrappers (mongo, redis, s3, firebase)
├── worker/           # Celery tasks and AI execution engine
│   ├── ai_executor.py         # Core AI-driven test executor (main orchestration)
│   ├── ai_provider.py         # OpenAI integration with circuit breaker
│   ├── intent_generator.py    # Generates test intents from AI
│   ├── intent_executor.py     # Executes intents via Playwright
│   ├── behavior_detection.py  # Detects test behavior patterns
│   ├── selector_engine/       # Selector resolution and healing
│   ├── recovery/              # Error recovery strategies
│   └── tasks/                 # Celery task definitions
├── intelligence/     # Cross-run analysis (flake detection, failure graphs, confidence scoring)
├── config.py         # Pydantic Settings — all config from environment variables
├── dependencies.py   # FastAPI dependency injection
└── main.py           # App entry point with lifespan management
```

**Execution flow:** API receives a test run request → Celery task queued → `ai_executor.py` orchestrates → `intent_generator.py` produces steps → `intent_executor.py` drives Playwright → evidence collected → confidence score computed.

**Configuration** (`app/config.py`) controls execution modes (FAST/STANDARD/DEEP), rate limits (3 concurrent per org, 20 queue depth, 10 global max), feature flags, and evidence lifecycle (90-day retention).

**Test fixtures** (`tests/conftest.py`) provide `mock_page`, `mock_ai_provider`, `failing_ai_provider`, `mock_db`, and sample intents. Tests do not use real Playwright or real AI calls.

## Frontend

**Stack:** Next.js 15 (App Router), React 19, TypeScript, Tailwind CSS v4, Firebase Auth

**Run dev server:**
```bash
cd frontend
npm install
npm run dev       # Development server on port 3000
npm run build     # Production build
npm run lint      # ESLint + TypeScript check
```

**Environment variables** (set at build time as `NEXT_PUBLIC_*`):
- `NEXT_PUBLIC_API_URL` — backend URL (http://localhost:8001 in dev)
- `NEXT_PUBLIC_FIREBASE_*` — Firebase project config

**Key structure:**
- `src/app/` — Next.js App Router pages
- `src/components/ui/` — Reusable UI primitives
- `src/components/` — Feature components (confidence-gauge, evidence-timeline, flake-badge, etc.)
- `src/contexts/auth-context.tsx` — Firebase auth state provider
- `src/types/` — Shared TypeScript interfaces

Path alias `@/*` maps to `src/*`.

## Infrastructure

MongoDB database is `qualora`. Collections and indexes are initialized by `infra/mongo/init-mongo.js` on first startup. MinIO default bucket is `qualora-artifacts`, created by `infra/minio/init-buckets.sh`.

The Celery Beat scheduler runs evidence pruning on an hourly schedule. Workers run with `--concurrency=1` (sequential) with a 30-minute soft task timeout.

## Workflow Orchestration

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately - don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity
- Break every plan into atomic steps - each step should do exactly one thing
- Identify dependencies between steps before starting - never assume order doesn't matter
- If the plan changes mid-execution, rewrite it fully - don't patch it
- Flag ambiguity before starting, not mid-task - ask clarifying questions upfront
- Time-box planning: if a plan takes longer than 10 minutes to write, the problem is not understood well enough

### 2. Subagent Strategy to Keep Main Context Window Clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution
- Never let the main context window become a dumping ground for exploration noise
- Subagents should return structured summaries, not raw dumps
- If a subagent fails, restart it with a clearer, narrower task - don't retry blindly
- Use subagents for: reading large files, running isolated tests, exploring unfamiliar codebases
- Main agent owns decisions - subagents own information gathering

### 3. Self-Improvement Loop
- After ANY correction from the user: update 'tasks/lessons.md' with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project
- Categorize lessons by type: logic errors, assumption errors, communication errors
- If the same mistake happens twice, escalate the lesson to a hard rule - no exceptions
- Track which lessons are applied per session - lessons not applied are lessons not learned
- Share lessons across similar projects when patterns repeat

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness
- Verification is not optional - it is the last step of every task
- If no tests exist, write a minimal one before closing the task
- Check edge cases explicitly: empty inputs, nulls, boundary values
- Confirm that your change does not break adjacent behavior
- Get a second opinion from a subagent when confidence is low

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes - don't over-engineer
- Challenge your own work before presenting it
- Elegance means: fewer moving parts, clearer intent, easier to delete later
- A solution is not elegant if only you can understand it - clarity is part of elegance
- If two solutions have equal correctness, always pick the simpler one
- Refactor as you go - leave the code cleaner than you found it, but don't go out of scope

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests -> then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how
- Reproduce the bug before fixing it - never fix what you haven't seen
- State the root cause before proposing a fix - symptoms and causes are different things
- If a bug has multiple possible causes, rule them out one by one - don't guess
- After fixing: confirm the fix doesn't introduce a regression
- Document what caused the bug in 'tasks/lessons.md' if it was non-obvious

### 7. Communication Protocol
- Always lead with the outcome, then explain the steps - not the other way around
- Use plain language - if a technical term is needed, define it first
- Never make the user ask "what did you just do?" - preempt that question
- Keep updates short during execution, detailed only at completion
- If blocked, say exactly what you need - one specific thing, not a list of options
- Silence is not an option - always confirm when a step is done

### 8. Scope Control
- Do exactly what is asked - nothing more, nothing less
- If you notice a nearby problem, flag it but don't fix it without permission
- Never refactor outside the stated scope, even if the code is messy
- Scope creep is a bug in your behavior - treat it as one
- If the scope is unclear, ask before starting - not halfway through

---

## Task Management

1. **Plan First**: Write plan to 'tasks/todo.md' with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review to 'tasks/todo.md'
6. **Capture Lessons**: Update 'tasks/lessons.md' after corrections
7. **Close the Loop**: Confirm with the user that all acceptance criteria are met before closing

---

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.
- **Ownership**: You own the outcome, not just the code. If something is broken after your change, it's your responsibility to fix it.
- **No Assumptions**: Never assume a file exists, a service is running, or a variable is set. Verify everything.
- **Reproducibility**: Every action you take should be reproducible by someone else following your steps.
- **Honesty Over Confidence**: If you don't know, say so. A wrong answer delivered confidently does more damage than an honest "I'm not sure."
