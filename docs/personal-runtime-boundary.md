# Personal runtime startup boundary

The Personal local runtime uses one composition module,
`src/personal_runtime.py`. Both shipped entry points select Personal by default;
there is no edition setting that disables its sync or changes its data formats.

The standalone stdio server starts readiness, installs the history integration
and queues its catch-up, reports the previous update, then registers update
preparation and the library sync trigger with `readiness.when_done`. As before,
those callbacks run when warmup finishes, including a failed warmup. Serving
starts without waiting for that work; exit still joins readiness before draining
queued logs and history. The installation lease bootstrap in `src/startup.py`
is unchanged. The Python helper `src.server.install_history_sync` remains
available through an import of the extracted function.

The daemon completes strict readiness before installing history. Its service
constructs the same `FlowsUI` and `UserSync` through the composition module and
retains the existing service attributes. Construction does not start sync or
read sync settings. The daemon's lifespan and admin routes still call the
existing start, wake, drain, resume and finish operations; `UserSync` retains
its queue, scheduler handoff, readiness gate, installation leases and final
sync cycle. The daemon continues to own its separate update lifecycle.

This extraction changes no tool schemas, Git sync settings, history approval or
export format, local flow Git versions, persisted state, or client registration.
The characterization checks in `tests/test_personal_runtime.py` use fake
startup boundaries and temporary service/library directories. Existing sync,
history, startup-lease and daemon suites remain the checks for their underlying
implementations.

Teams remains separate work. A hosted engine still needs an explicit server
composition and lifecycle, authenticated membership and grants, server-side team
storage, an opaque personal-sync store, and a thin Rust client bridge for transport, grants,
cryptography and minimal local vault I/O. This module does not provide that
runtime or make the current Python server safe to expose as a Teams service.
No local Python Core, model, team index or Git is added to the Teams client.

## Explicit composition points

`Service` and `create_app` accept a keyword-only `components_factory`. It is
called once per service with that service and the unchanged
`hand_off_schedule` flag, and returns `DaemonComponents(flows_ui, user_sync)`.
Without an override, `personal_runtime.daemon_components` constructs the same
Personal objects. With an override, the service does not also construct the
default editor or another sync owner. The supplied objects must support the
existing editor and sync lifecycle APIs, including health, admin routes and
bounded shutdown. `DaemonEditor` and `SyncLifecycle` describe the operations the
daemon calls. Reusing the Personal editor also requires the separate
`PersonalEditorSync` contract: `perform`, `offload`, `engine` and `status_view`.
The editor and sync object must therefore be supplied as a compatible pair.
The factory runs before `service.artifacts` and `service.local_inference` are
assigned and must not access those attributes during construction or start work.

`load_runtime` separately accepts a `history_sync_installer` hook, called only
after strict readiness and transport configuration. Its default is the existing
Personal history integration. A composition can supply a loader bound to its own
hook without changing the daemon's existing one-argument loader contract.
An injected hook owns its error policy: an uncaught exception fails daemon
startup, leaving health unavailable. The default Personal hook retains its
existing warning-and-continue behavior when history sharing is unavailable;
there is no automatic fallback to it after an injected hook fails.

`src/stdio_runtime.py` runs an explicitly supplied transport, readiness worker,
startup hook and log drain. It selects no updater, Git adapter or profile.
`src/server.py` supplies the Personal startup hook and the existing shutdown
timeout. Readiness still starts before history sync and deferred update/sync
callbacks; shutdown joins readiness before draining logs. An exception from a
supplied startup hook now also takes that cleanup path before propagating.

These composition points run after the existing installation bootstrap. They
do not move application imports ahead of installation leases, change
`configure_runtime_paths`, or make import-time engine state request-scoped.
Storage/access isolation and an actual second server composition remain later
work. The factory alone does not provide a Git-free or tenant-safe Teams server.

`tests/test_runtime_composition.py` checks that an injected daemon composition
reaches its UI and health routes without constructing the Personal default,
and lifecycle, and that stdio joins and drains on both startup and transport
failures. Hook tests also check transport configuration and exception propagation. The
existing Personal characterization and sync suites cover the default lifecycle.
