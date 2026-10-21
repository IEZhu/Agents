# Personal runtime startup boundary

This first extraction gives the existing Personal local runtime one startup
composition module, `src/personal_runtime.py`. Personal remains the only runtime
composed by both entry points. There is no edition setting or switch that skips
Personal startup.

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
