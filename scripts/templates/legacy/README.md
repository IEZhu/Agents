# Previously generated routing reminders

Exact copies of routing memory files that earlier installers wrote. The migration
helper compares an installed reminder with these bytes: a match is replaced with
the current `../memory-routing.md`, anything else is preserved as user-edited.
Do not edit these files.

Before changing `../memory-routing.md`, copy its current bytes here unchanged as
`memory-routing-v<N>.md`, using the next unused number. The helper reads only
files matching `memory-routing-*.md`; without that copy, installed reminders of
the previous version are reported as user-edited and are not migrated. Also add
the new copy where the tests list the existing ones
(`grep -rn memory-routing-v tests/`).
