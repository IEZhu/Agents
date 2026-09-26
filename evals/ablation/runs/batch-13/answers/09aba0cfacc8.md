Pin these versions:

```text
authkit==2.3
telemetry==3.1
httpcore==4.1
cfgload==1.9
```

This is the only set of versions that meets both of your minimums, so it is also the newest set that works. Neither of your top-level packages can use its latest release.

## How I got there

Your requirements limit the choices to authkit ∈ {2.3, 2.4} and telemetry ∈ {3.1, 3.2}.

1. **authkit 2.4 can't be used.** It needs `httpcore >=5.0`, so httpcore has to be 5.0. Both telemetry versions you can use need `httpcore <5.0` (3.2 says `<5.0`, 3.1 says `<4.2`). That's a direct conflict, so **authkit = 2.3**.
2. **authkit 2.3 decides cfgload.** It needs `cfgload >=1.9`, so **cfgload = 1.9**. It also needs `httpcore >=4.1,<5.0`, which leaves httpcore 4.1 or 4.2.
3. **telemetry 3.2 can't be used.** It needs `cfgload <=1.8`, which conflicts with cfgload 1.9 from step 2. So **telemetry = 3.1**.
4. **telemetry 3.1 decides httpcore.** It needs `httpcore <4.2`. Combined with authkit's `>=4.1`, that leaves **httpcore = 4.1**. Its `cfgload >=1.8` is satisfied by 1.9.
5. **Check httpcore 4.1's own dependency.** It needs `cfgload >=1.9`, and 1.9 satisfies it. Everything is consistent.

| Package | Pinned | Constraints it satisfies |
|---|---|---|
| authkit | 2.3 | yours (>=2.3) |
| telemetry | 3.1 | yours (>=3.1) |
| httpcore | 4.1 | authkit 2.3 (>=4.1,<5.0), telemetry 3.1 (<4.2) |
| cfgload | 1.9 | authkit 2.3 (>=1.9), telemetry 3.1 (>=1.8), httpcore 4.1 (>=1.9) |

## Things to watch

- **Reading of `;`:** I took `;` in your list to separate two requirements. In a real PEP 508 dependency string, `;` starts an environment marker (for example `; python_version < "3.11"`). If the actual pyproject strings really look like `httpcore >=4.1,<5.0 ; cfgload >=1.9`, the metadata is malformed. That alone could explain odd resolver behavior, so check the published metadata.
- **This set is fragile.** It's the only combination that works, so any future change to these constraints could break it. The blockers are telemetry's upper caps: `httpcore <4.2` on 3.1, and `httpcore <5.0` plus `cfgload <=1.8` on 3.2. They keep you off authkit 2.4. Relaxing those caps in telemetry is the change that would let you upgrade.
- **The timeouts:** a set of mutually exclusive upper and lower bounds like this makes a resolver backtrack. It's plausible that this contributes to the timeouts, but the index itself could also be slow, and I can't tell which from here. If you keep full pins, installing with `pip install --no-deps -r requirements.txt` skips resolution completely. Run `pip check` afterwards to confirm the environment is consistent.

**Confidence: HIGH.** I checked all four authkit/telemetry combinations against the metadata you gave. Three fail on a hard conflict, and the one that remains is fully satisfiable. This depends on your list being complete and on reading `;` as separating two requirements.
