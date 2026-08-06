# CUE backend

FastAPI + SQLAlchemy (async) + Alembic service implementing CUE's commitment ledger:
extracting commitments from vendor messages, enforcing provenance (every commitment must
cite an exact-substring evidence span, verified in code and at the database layer), and
serving the ledger API. See `../CUE-PRD.md` for the full product spec and `../CLAUDE.md`
for the extraction-tuning discipline.

## Quickstart

```bash
cp .env.example .env
docker compose up -d --wait   # Postgres, with the cue_app role provisioned
uv sync
uv run pytest                 # against a real cue_test database
```

Every endpoint requires a bearer token (`Authorization: Bearer <jwt>`) — see
`app/identity/tokens.py`. Locally, `CUE_AUTH_PROVIDER=local` (the default) mints
and verifies self-issued tokens against a shared secret; `mint_local_token` is
what `tests/conftest.py` uses, and the same call works for manual local dev:

```bash
uv run python -c "
import uuid
from app.identity.tokens import mint_local_token
print(mint_local_token(subject='dev', org_id=uuid.uuid4(), secret='dev-only-insecure-secret-change-me'))
"
```

Note the `org_id` must match a real `organisations` row (there's no
self-serve org creation endpoint — tenant provisioning is out of scope this
build slice, per PRD §6.14's SCIM note) and the token's subject/org resolve
to a `users` row on first use (`app/identity/service.py`'s `resolve_user`).

`tests/conftest.py` creates and migrates `cue_test` itself on first run — no separate
migration step needed for tests. For the running app, apply migrations to the dev
database directly:

```bash
uv run alembic upgrade head
uv run fastapi dev main.py
```

## Layout

| Path | What's there |
|---|---|
| `app/core/` | Settings, DB engine/session, the shared SQLAlchemy declarative `Base` (`app/core/base.py`) |
| `app/identity/` | Identity/RBAC (PRD §6.14): JWT verification (provider-pluggable, Authlib), `users`/`memberships`/`delegations` models, role resolution |
| `app/api/deps.py` | Per-request auth: verifies the bearer token, sets RLS's `app.current_org_id`, enforces project membership + role |
| `app/llm/` | Model client + provider routing (Ollama locally, Anthropic in production) |
| `app/ledger/` | Extraction: prompt/schema loading, code-level evidence verification |
| `app/models/` | SQLAlchemy models (RLS-enforced, multi-tenant) |
| `app/capture/` | Fixture-based message capture (stand-in until real channel capture ships) |
| `cue-eval/` | Extraction prompt/schema tuning harness — see its own README |
| `scripts/extract_fixtures.py` | Runs cue-eval's fixture cases through the real pipeline |

## CI

- `.github/workflows/pytest.yml` — required gate, runs `tests/` against real Postgres on
  every push/PR.
- `.github/workflows/cue-eval.yml` — informational only, runs the extraction eval against
  local Ollama (`qwen2.5:14b`) when `cue-eval/` changes. Never blocks a merge; see the
  workflow file for why.
