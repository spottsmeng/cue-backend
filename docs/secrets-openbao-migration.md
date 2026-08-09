# Secrets: `.env` → OpenBao migration path

NFR-SEC-03 (PRD §7.4): "Secrets in a managed vault; no credential in code or
configuration files." CUE-Tech-Stack.md §2.6 names **OpenBao** (the
post-relicense Vault community fork, MPL 2.0) as the production choice —
"per-tenant key separation lives here." This document is that migration
path: what moves, where it moves to, and how the running application picks
it up — **deployment configuration, not new application code** (per Prompt
13's own framing for this item). `app/`'s settings classes already read
plain environment variables (`pydantic-settings`, `env_prefix=...`) and stay
completely unaware of where those variables ultimately come from — that's
what makes this a config-layer change rather than a code change.

## What's actually in `.env` today

`.env.example` (refreshed alongside this doc) enumerates the real surface —
~50 variable names across nine categories, drawn directly from this
session's own working `.env`, not guessed. Three tiers, by sensitivity:

| Tier | Examples | OpenBao? |
|---|---|---|
| **Real secrets** — leak = compromise | `CUE_LLM_ANTHROPIC_API_KEY`, `CUE_STORAGE_SECRET_KEY`, `CUE_MATTERMOST_BOT_TOKEN`, `CUE_IMAP_SMTP_PASSWORD`, `CUE_*_APP_PASSWORD`, `CUE_GRAPH_CLIENT_SECRET`, `CUE_WHATSAPP_API_TOKEN`, `CUE_WECHAT_CORP_SECRET`, `CUE_AUTH_LOCAL_JWT_SECRET` | Yes |
| **Connection config** — not secret alone, but ties to a specific deployment | `CUE_DATABASE_URL` (host/port/db name; password component is a secret, see below), `CUE_STORAGE_ENDPOINT`, `CUE_MATTERMOST_BASE_URL`, `CUE_LLM_OLLAMA_BASE_URL` | No — stays in ordinary deployment config (Helm values, ECS task def, etc.) |
| **Behavioural switches** — which code path runs, not a credential | `CUE_SHAREPOINT_PROVIDER`, `CUE_CAPTURE_BACKEND`, `CUE_AUTH_PROVIDER`, `CUE_LLM_EXTRACTION_PROVIDER` | No |

Only the first tier is what NFR-SEC-03/OpenBao is actually about. Migrating
the other two tiers into a vault would be security theatre — they're not
secrets, and hiding them there makes ordinary deployment debugging harder
for no isolation benefit.

## KV layout

OpenBao's KV v2 secrets engine, one mount per environment, paths grouped by
the same categories `.env.example` already uses:

```
secret/cue/<environment>/llm/anthropic_api_key
secret/cue/<environment>/auth/local_jwt_secret          # "local" auth only; unset once OIDC is live
secret/cue/<environment>/storage/access_key
secret/cue/<environment>/storage/secret_key
secret/cue/<environment>/sharepoint/graph_client_secret
secret/cue/<environment>/sharepoint/nextcloud_app_password
secret/cue/<environment>/graph/client_secret
secret/cue/<environment>/whatsapp/api_token
secret/cue/<environment>/wechat/corp_secret
secret/cue/<environment>/wechat/session_archive_secret
secret/cue/<environment>/mattermost/bot_token
secret/cue/<environment>/imap_smtp/password
secret/cue/<environment>/nextcloud/app_password
```

`<environment>` (e.g. `dev`, `staging`, `prod-sg`, `prod-cn`) is where
NFR-SCL-03/NFR-PRV-07's per-region data-residency requirement and OpenBao's
own "per-tenant key separation" pitch actually connect — a region gets its
own path prefix and its own OpenBao namespace/policy, so a credential
compromise in one region's deployment cannot reach another's. This is
exactly the per-region split NFR-SCL-05/NFR-PRV-07 already need at the
infrastructure level (see below) — OpenBao's namespace boundary should
follow the same lines, not a separate one.

## How the running app picks this up — no application code change

Two standard patterns, either works with zero changes to `app/*/config.py`:

1. **OpenBao Agent, template mode** — a sidecar/init container renders a
   `.env`-shaped file from the paths above (Agent's own templating
   language) before the app process starts; `docker-entrypoint.sh` (or the
   container's `CMD`) sources it, then execs `uvicorn`/`arq`. The
   settings classes never know the values didn't come from a real `.env`
   file — `pydantic-settings`'s `env_file=".env"` loading and plain
   `os.environ` reads are indistinguishable to it.
2. **OpenBao Agent, env-injection mode** — the sidecar populates the
   process environment directly (Kubernetes: an init container writing to
   a shared `emptyDir`-mounted env file, or a CSI secrets-store-driver
   volume), same end state.

Either way, the swap is a change to the container/pod spec (which secrets
engine paths map to which env vars) and the deployment pipeline, not to
`app/llm/config.py`, `app/documents/config.py`, `app/capture/config.py`, or
any other settings class in this codebase.

## What this session actually stood up

A local, dev-mode-only OpenBao container (`docker-compose.yml`, `profiles:
[observability]` — same opt-in pattern as the `otel` service; **not** part
of `docker compose up -d --wait`, so it never affects `pytest.yml`'s CI
run) plus an example policy (`docs/openbao/cue-backend-policy.hcl`)
granting read-only access to `secret/data/cue/dev/*`. This proves the KV
layout and policy shape are sound; it deliberately does **not** wire a real
Agent sidecar into `main.py`'s own startup, since that's exactly the
"deployment configuration, not new application code" boundary this
document is drawing — a real Agent/init-container setup belongs in
whatever deployment manifests (Helm chart, ECS task definition) eventually
carry this app to a real environment, none of which exist in this repo yet.

## Explicitly not attempted here

- **Automatic secret rotation** — OpenBao supports dynamic secrets (e.g.
  short-lived DB credentials) for some backends; `CUE_DATABASE_URL`'s
  static `cue_app` password could eventually move to OpenBao's database
  secrets engine instead of a KV read, but that's a real schema/ops change
  (the DB role's auth method changes), not something to bundle into this
  pass.
- **Per-region OpenBao clusters** — this doc names the path/namespace
  convention regional isolation should follow; actually standing up
  multiple regional OpenBao deployments is the same NFR-SCL-05/NFR-PRV-07
  "Kubernetes/ArgoCD-level deployment topology" this milestone's own
  EXPLICITLY OUT OF SCOPE section already excludes.
