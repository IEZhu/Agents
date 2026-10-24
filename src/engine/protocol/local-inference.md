<!-- variant: full -->
For an optional local completion, pass the same complete manifest to
`local_context_create.artifact_manifest`. The broker hydrates all selected
entries exactly before byte and native-token budget checks, and rechecks the
manifest before dispatch, publication and result reads. Reuse the same context
with separate grants when sharing a prefix; `local_generate` is one completion,
not a tool loop or an artifact reader. Oversized input must fail without
summarization, truncation or replay. This does not replace native workers or
the prepare/validate exchange.

<!-- variant: compact -->
local_context_create.artifact_manifest hydrates all selected entries exactly before byte/native-token limits and rechecks before dispatch, publication and result reads. Reuse one context with separate grants for shared prefixes; local_generate is one completion, not a tool loop or artifact reader. Oversized input fails without summarization, truncation or replay. This does not replace native workers or the prepare/validate exchange.
