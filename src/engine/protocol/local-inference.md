<!-- variant: compact -->
Optional owner-enabled local inference tools run separate bounded completions through the shared HTTP daemon; they do not replace persona activation or the prepare/validate worker exchange. Read local_context_create's description before the first one.

<!-- variant: tool -->
Local inference is an optional, owner-enabled service of the shared HTTP daemon. For a local completion over shared task data, pass the same complete artifact manifest as artifact_manifest. The broker hydrates all selected entries exactly before byte and native-token budget checks, and rechecks the manifest before dispatch, publication and result reads. Reuse one context with separate grants when sharing a prefix; local_generate is one completion, not a tool loop or an artifact reader. Oversized input fails without summarization, truncation or replay. A local completion does not replace persona activation, native worker spawning or the prepare/validate exchange.
