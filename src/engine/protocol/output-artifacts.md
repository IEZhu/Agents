<!-- variant: full -->
When the assignment authorizes output artifacts, workers publish them and create
separate output manifests with the contract task ID. Return exact references as
`artifact://<manifest_id>/<manifest_sha256>/<artifact_id>` in `artifacts[].path`.
These are tool references, not MCP resource endpoints. The parent reads and
verifies them before acceptance, while retaining the original input contract.
Read-only tasks gain no publication permission; keep output manifests active
until the parent consumes them.

<!-- variant: compact -->
When the task authorizes output artifacts, publish them under a separate output manifest with the contract task ID; return artifact://<manifest_id>/<manifest_sha256>/<artifact_id> in artifacts[].path. These are tool references, not MCP resources. The parent reads and verifies them while retaining its original input contract. Keep output manifests active until consumed; read-only tasks gain no publication permission.
