# CUE backend

FastAPI + SQLAlchemy (async) + Alembic service implementing CUE's commitment ledger:
extracting commitments from vendor messages, enforcing provenance (every commitment must
cite an exact-substring evidence span, verified in code and at the database layer), and
serving the ledger API. See `../CUE-PRD.md` for the full product spec and `../CLAUDE.md`
for the extraction-tuning discipline.

## Quickstart

```bash
cp .env.example .env
docker compose up -d --wait   # Postgres, MinIO, Valkey, with the cue_app role provisioned
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
| `app/twin/` | Production Twin (PRD §6.8): CPM slack/critical-path, archetype seeding, hypothetical-shift propagation |
| `app/documents/` | Documents (PRD §6.6): versioned storage, OCR/parsing stand-in, spec-claim extraction |
| `app/foresight/` | Foresight (PRD §6.9/§6.7/§6.15): Silence Radar, contradiction/spec-drift detection, forecast heuristic, escalation, Deviations, notification core — see its own section below |
| `app/ask/` | Ask & retrieval (PRD §6.11): embedding client (Ollama/TEI), hybrid lexical+semantic retrieval, query/summarise/successor-brief composers, follow-up sessions |
| `cue-eval/` | Extraction prompt/schema tuning harness — see its own README |
| `scripts/extract_fixtures.py` | Runs cue-eval's fixture cases through the real pipeline |

## Background jobs (arq + Valkey)

Foresight (Prompt 7) is the first milestone that needs a scheduled/background job — Silence
Radar, forecasting, the FR-LCY-02/03 automatic lifecycle transitions, escalation and
notification delivery all have to run periodically, not just on request. `app/foresight/worker.py`
is an [arq](https://arq-docs.helpmanual.io/) worker (CUE-Tech-Stack.md §2.4: "Lightweight task
queue — arq (Valkey-backed)"), connected to the `valkey` service `docker compose up` already
starts (same health-checked, named-volume shape as `postgres`/`minio` — see `docker-compose.yml`).

Run the worker locally:

```bash
uv run arq app.foresight.worker.WorkerSettings
```

It registers three cron jobs, all on the same 15-minute schedule: `run_foresight_sweep` — for every
non-archived project across every organisation, in order: Silence Radar, contradiction/spec-drift
detection, the forecast heuristic, the FR-LCY-03 due-time-passed sweep, escalation, and webhook
delivery of any notification past its `deliverable_at` (FR-NTF-04); `run_due_report_schedules`
(FR-RPT-09); and `run_embedding_sweep` (`app/ask/embed_worker.py`) — populates
`DocumentVersion.embedding` and `RetrievalChunk` rows (Evidence/AuditLog text) for Ask's hybrid
retrieval, per project, in bounded batches. Each is a plain async function with no queue-specific
dependency on its `ctx` argument, so all three are also directly callable — by tests, or a one-off
ops invocation — without a running worker or broker at all.

Connection settings (`app/foresight/config.py`'s `ArqSettings`) default to `docker-compose.yml`'s
`valkey` service (`localhost:6379`) and are overridable via `CUE_ARQ_REDIS_HOST`/`CUE_ARQ_REDIS_PORT`,
same env-prefixed-settings shape as `app/documents/config.py`'s `StorageSettings`.

**Escalation note**: CUE-Tech-Stack.md §2.4 names Temporal, not arq, for durable "escalation
chains... long-running, resumable, human-in-the-loop flows." This session deliberately does not
add Temporal — a second, heavier piece of infrastructure this milestone's actual scope doesn't
need — in favour of a periodic reconciliation sweep (`app/foresight/escalation.py`), which is
sufficient at this scale. Revisit if/when escalation needs genuinely durable, multi-day workflow
state a stateless sweep can't express.

## CI

- `.github/workflows/pytest.yml` — required gate, runs `tests/` against real Postgres on
  every push/PR.
- `.github/workflows/cue-eval.yml` — informational only, runs the extraction eval against
  local Ollama (`qwen2.5:14b`) when `cue-eval/` changes. Never blocks a merge; see the
  workflow file for why.
