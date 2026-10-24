<!-- variant: full -->
## Describe a repository on first work

The first time a conversation works in a repository (reads, searches or changes
its files), call `describe_repo` when that tool is available, with `workspace`
(and `repo_path` for a subdirectory) before exploring it ad hoc. On
`needs_summary`, follow its `instruction`; call `write_repo_summary` only where
that instruction allows a write. If the tool is unavailable, report that
limitation and use the documented manual role fallback without claiming a
loaded repository description.

<!-- variant: compact -->
For ordinary conversations, the first time you read, search or change repository files, when describe_repo is available, call describe_repo with workspace and the target repo_path before ad hoc exploration. On needs_summary, follow its instruction; call write_repo_summary only where that instruction allows a write.

<!-- variant: compact-unavailable -->
When describe_repo is unavailable, report that limitation and use the documented manual role fallback without claiming a loaded repository description.
