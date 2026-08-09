# Example OpenBao policy for the cue-backend service identity — read-only
# access to this environment's own secret path, nothing else. Apply with:
#   bao policy write cue-backend-dev cue-backend-policy.hcl
# then attach it to whatever auth method actually authenticates the
# deployed app (AppRole, Kubernetes auth, etc. — not decided here, see
# docs/secrets-openbao-migration.md).
#
# Path matches docs/secrets-openbao-migration.md's KV v2 layout
# (secret/cue/<environment>/<category>/<key>) — "dev" here, one policy per
# environment in a real deployment, per that doc's per-region isolation note.
path "secret/data/cue/dev/*" {
  capabilities = ["read"]
}

path "secret/metadata/cue/dev/*" {
  capabilities = ["list", "read"]
}
