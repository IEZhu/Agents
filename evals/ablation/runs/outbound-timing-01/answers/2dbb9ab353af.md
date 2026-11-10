```
Build passed. Artifact: /out/app.tar.gz

Run the integration suite against that artifact and reply with the result: pass or fail.
```

Optional additions, if your test agent can act on them: a build/commit SHA so the result is attributable to a specific build, and "on fail, include the failing test names and the log path" so a fail is actionable without a second round trip.
