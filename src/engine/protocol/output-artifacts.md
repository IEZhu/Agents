<!-- variant: tool -->
When the assignment authorizes output artifacts, workers publish them under a separate output manifest with the contract task ID and return artifact://<manifest_id>/<manifest_sha256>/<artifact_id> in artifacts[].path: tool references, not MCP resource endpoints. Read and verify them before acceptance while retaining the original input contract. Read-only tasks gain no publication permission; keep output manifests active until the parent consumes them.

<!-- variant: worker -->
A worker whose assignment authorizes output artifacts publishes each one under its own output
manifest, created with the contract's task ID, and lists it as
`artifact://<manifest_id>/<manifest_sha256>/<artifact_id>` in `artifacts[].path`. It keeps that
manifest active for the parent and never replaces or releases the parent's input manifest.
