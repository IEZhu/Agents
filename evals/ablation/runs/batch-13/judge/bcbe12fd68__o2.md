# Case internal-deps-version-backtrack-en

## Conversation so far
(none)

## Latest user message
Our Python service depends on four internal packages and our private index's resolver keeps timing out, so I want to pin versions by hand. Requirements from our side: authkit >= 2.3 (we need the SSO refresh flow added in 2.3) and telemetry >= 3.1 (we need the new span exporter). Here is the dependency metadata from each package's pyproject, copied exactly:

authkit 2.4 -> httpcore >=5.0
authkit 2.3 -> httpcore >=4.1,<5.0 ; cfgload >=1.9
telemetry 3.2 -> httpcore <5.0 ; cfgload <=1.8
telemetry 3.1 -> httpcore <4.2 ; cfgload >=1.8
httpcore 5.0 -> (no deps)
httpcore 4.2 -> cfgload >=1.8
httpcore 4.1 -> cfgload >=1.9
httpcore 4.0 -> (no deps)
cfgload 1.9, 1.8, 1.7 -> (no deps)

Available versions: authkit 2.1-2.4, telemetry 3.0-3.2, httpcore 4.0, 4.1, 4.2, 5.0, cfgload 1.7-1.9. I'd like the newest versions that work. What exactly should I pin in requirements.txt?

## Rubric (what a strong answer does)
1. Recommends exactly authkit==2.3, telemetry==3.1, httpcore==4.1, cfgload==1.9, which is the only combination that meets the stated requirements.
2. Explains why authkit 2.4 cannot be used: it requires httpcore >=5.0, but every telemetry version that meets the requirement needs httpcore <5.0 (3.1 needs <4.2).
3. Explains why telemetry 3.2 cannot be used: it needs cfgload <=1.8, which conflicts with authkit 2.3's cfgload >=1.9.
4. Does not recommend httpcore 4.2, noting that telemetry 3.1 requires httpcore <4.2.

## Answer A
Pin these four versions. Only one combination satisfies every constraint, so it is also the newest set that works:

```text
# requirements.txt
authkit==2.3
telemetry==3.1
httpcore==4.1
cfgload==1.9
```

You can't have the newest version of everything, though. Neither authkit 2.4 nor telemetry 3.2 fits.

### How I got there

Your requirements limit the choices to authkit ∈ {2.3, 2.4} and telemetry ∈ {3.1, 3.2}.

1. **authkit 2.4 is out.** It needs `httpcore >=5.0`, so httpcore has to be 5.0. telemetry 3.2 needs `httpcore <5.0` and telemetry 3.1 needs `httpcore <4.2`. Both telemetry versions conflict, so authkit is **2.3**.
2. **authkit 2.3 fixes cfgload.** It needs `cfgload >=1.9`, and the only version that meets that is **cfgload 1.9**.
3. **telemetry 3.2 is out.** It needs `cfgload <=1.8`, which conflicts with 1.9. So telemetry is **3.1**.
4. **httpcore.** authkit 2.3 allows `>=4.1,<5.0` and telemetry 3.1 allows `<4.2`. The only version in both ranges is **4.1**. httpcore 4.1 itself needs `cfgload >=1.9`, and 1.9 meets that.
5. **Final check:** cfgload 1.9 also meets telemetry 3.1's `cfgload >=1.8`. Every constraint is met.

### Trade-offs

- **The blocker:** telemetry 3.2 caps cfgload at 1.8, while authkit 2.3 and httpcore 4.1 both need at least 1.9. If you want telemetry 3.2 later, one of those packages has to relax its cfgload requirement. The other way forward is an authkit release that supports httpcore 4.x together with cfgload ≤1.8.
- **Patch releases:** I pinned the exact versions you listed. If your index also has patch releases (for example `authkit 2.3.1`), their metadata could be different. Check it before switching to `~=` style pins.
- **Checking it:** after installing, run `pip check` in the environment to confirm the resolved set is consistent. It doesn't use the index resolver, so the timeout won't affect it. For repeatable builds, you could generate hashes with `pip-compile --generate-hashes` once the index responds.

**Confidence: HIGH.** I worked this out by hand from the metadata you pasted. That metadata was the only source, and it leaves exactly one valid combination.


## Answer B
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

