# Case en-liveness-readiness

## Conversation so far
(none)

## Latest user message
What's the actual difference between readiness and liveness probes in Kubernetes? We had a liveness probe hitting /health that checks the database, and during a DB outage all our pods restarted in a loop. Why, and how should we set it up?

## Rubric (what a strong answer does)
1. A failing readiness probe removes the pod from Service endpoints without a restart; a failing liveness probe makes the kubelet restart the container.
2. Explains that the database check in the liveness probe turned a dependency outage into restarts of every pod, and keeps liveness to the process itself.
3. Mentions a sensible setting such as a startup probe, initialDelaySeconds or failureThreshold.

## Answer A
**Short version:** your liveness probe checked a dependency. When the DB went down, every pod failed liveness at the same moment, so the kubelet killed and restarted all of them. Restarting a pod can't fix a database, so the restarted pods failed again and went into CrashLoopBackOff. A liveness probe should answer only one question: "is this process broken in a way that a restart would fix?"

## What each probe does

| | Liveness | Readiness |
|---|---|---|
| Question it answers | Is the process alive, or is it stuck? | Can this pod serve traffic right now? |
| Action on failure | The kubelet **kills and restarts** the container | The pod is **removed from Service endpoints**, so it gets no traffic. Nothing is restarted. |
| Recovery | Only through a restart | Automatic: once the probe passes again, traffic comes back |
| Good things to check | Deadlocked event loop, hung worker threads, a corrupted internal state the app can't recover from | Warm-up finished, critical dependencies reachable, not overloaded |

There's also a third probe, the **startupProbe**. Until it succeeds, the liveness and readiness probes don't run. It's for slow-starting apps, so you don't have to give liveness a large `initialDelaySeconds`.

## Why you got the restart loop

1. The DB goes down, and `/health` on every replica fails at about the same time, because they all share that dependency.
2. After `failureThreshold` consecutive failures, the kubelet restarts each container. The default is 3 failures, with probes every 10s (recalled defaults; confirm in the K8s probe docs for your version).
3. The containers come back up and the DB is still down. They fail liveness again, get restarted again, and the restart backoff keeps growing. That's CrashLoopBackOff.
4. Side effects that make things worse:
   - The pods all restart together, so the cache warm-up and connection-pool setup are lost.
   - When the DB recovers, every pod reconnects at once, which is a thundering herd against a DB that has only just come back.
   - Each restart can also wait for the slow startup again, so your recovery takes longer than the DB outage did.

The same kind of failure (one shared dependency hitting all replicas at once) happens even with readiness alone, just in a milder form. If readiness checks the DB, every pod goes NotReady together, and the Service has zero endpoints. For a hard dependency that may be correct, since the requests would fail anyway. But clients then get connection refused or a 503 from the ingress instead of a clean error from your app. Often it's better to stay Ready and return a fast, explicit 503 or a degraded response from the app itself. Which is better depends on whether some of your endpoints still work without the DB.

## How to set it up

Split the endpoints:

- **`/livez`**: in-process only. It returns 200 if the HTTP server can respond and, if you have one, a cheap internal watchdog is fine (for example, the main loop or worker heartbeat ticked within the last N seconds). **No DB, cache, or downstream calls.**
- **`/readyz`**: startup is complete, plus checks on the dependencies this pod truly can't serve without. Keep these checks cheap: use a pooled connection with a short timeout, or better, read a cached status that a background checker updates every few seconds. That way probe traffic doesn't hammer the DB.

```yaml
containers:
  - name: api
    image: registry.example.com/api:1.4.2   # pinned tag (or digest)
    ports:
      - name: http
        containerPort: 8080
    startupProbe:
      httpGet:
        path: /livez
        port: http
      periodSeconds: 5
      failureThreshold: 30        # up to ~150s to start before liveness takes over
    livenessProbe:
      httpGet:
        path: /livez              # process-local only, never touches the DB
        port: http
      periodSeconds: 10
      timeoutSeconds: 2           # default is 1s (recalled) - too tight under GC/CPU throttling
      failureThreshold: 3         # ~30s of hard failure before a restart
    readinessProbe:
      httpGet:
        path: /readyz             # may check the DB, ideally via cached status
        port: http
      periodSeconds: 5
      timeoutSeconds: 2
      failureThreshold: 2         # drop out of rotation quickly
      successThreshold: 1
```

Practical notes:

- **Liveness should be more lenient than readiness.** A false positive on liveness kills a pod. A false positive on readiness only pauses its traffic for a moment.
- **Don't use the same endpoint for both probes** unless that endpoint really is process-local.
- **Set probe timeouts below your app's internal dependency timeouts.** Then a slow DB call can't make `/livez` hang. This only works if `/livez` doesn't share a saturated thread pool with request handling. If it does, a request backlog can still starve it, so serve health checks on a lightweight path.
- **Add a PodDisruptionBudget, and handle DB unavailability in the app** (a circuit breaker, connection retries with backoff and jitter). Probes are not a resilience mechanism for dependencies.
- If you're on Spring Boot, its actuator already has separate liveness and readiness groups (`/actuator/health/liveness` and `/actuator/health/readiness`). Recalled, not verified: check the docs for your version. By default, the liveness group doesn't include the DB indicator. This is a common trap: the generic `/actuator/health` *does* include it.

## Rolling it out

- **Deploy:** ship the `/livez` and `/readyz` endpoints first, then change the probe config in the Deployment. Use a normal rolling update with `maxUnavailable: 0` if you have the capacity for it.
- **Verify:**
  - `kubectl describe pod <pod>`: check that the probe config is correct and that the events show no `Unhealthy` or `Killing` entries.
  - `kubectl get endpoints <svc>`: during the test, check whether pods drop out of the Service.
  - In staging, block DB access, for example with a NetworkPolicy or by scaling the DB down. Expected result: pods go NotReady (or keep serving 503s, depending on the choice above), **`RESTARTS` stays flat**, and when the DB comes back, pods return to Ready without restarting.
- **Blast radius:** a misconfigured probe on this Deployment can make all its pods NotReady (an outage for this service) or put them into a restart loop. It doesn't affect other workloads.
- **Rollback:** `kubectl rollout undo deployment/<name>`, or revert the manifest commit if you deploy through GitOps. The new endpoints are additive, so leaving them in place after a rollback is harmless.


## Answer B
**Short answer:** a liveness probe answers "is this process broken beyond repair, so kill it?" A readiness probe answers "should this pod get traffic right now?" Your liveness probe checked a dependency (the database). When the DB went down, every pod failed liveness at the same moment, so the kubelet restarted all of them. The restarts couldn't fix the DB, so the pods kept failing and restarting, and you got a fleet-wide CrashLoopBackOff. **Never put external dependencies in a liveness check.**

## What each probe does

| | Liveness | Readiness | Startup |
|---|---|---|---|
| Question | Is the process wedged (deadlock, hung event loop)? | Can it serve requests right now? | Has it finished booting? |
| On failure | kubelet **kills and restarts the container** | Pod is **removed from Service endpoints**. No restart. | Container is killed if it never succeeds within the budget |
| Runs | Whole lifetime (after startup succeeds) | Whole lifetime | Only until first success; liveness/readiness are held off until then |
| Should check | Only the process's own internal health | Things the pod needs to serve traffic, used carefully | The same thing as liveness, with a longer budget |

The rule of thumb: **liveness failure should mean "a restart will fix this."** A DB outage fails that test. Restarting your app doesn't bring Postgres back, and a stampede of restarting pods that all reconnect and warm caches at once can make DB recovery harder.

## Why the loop happened

1. DB goes down, so `/health` returns 5xx (or times out) on every pod.
2. After `failureThreshold` consecutive failures, the kubelet kills each container.
3. The containers restart, the DB is still down, and the probes fail again. Restart backoff grows exponentially, capped at about 5 minutes (recalled, not verified; check the docs for your version).
4. When the DB comes back, pods may be stuck in backoff, so recovery takes longer than the outage did. All of them also reconnect at once.

A short timeout makes this worse. `timeoutSeconds` defaults to 1s (recalled, not verified), so a DB check that hangs on a connection attempt fails the probe even when the DB is merely slow.

## How to set it up

Split the endpoints:

- `/livez`: returns 200 if the process can answer HTTP at all. No DB, no downstream calls. At most, check internal state such as "the worker thread has heartbeated in the last N seconds."
- `/readyz`: returns 200 when the pod can usefully serve, meaning startup is done, it is not draining, and its *own* critical resources are OK.

```yaml
containers:
  - name: api
    image: registry.example.com/api:1.8.3   # pin the tag (or digest)
    ports:
      - containerPort: 8080
    startupProbe:
      httpGet: { path: /livez, port: 8080 }
      periodSeconds: 5
      failureThreshold: 30        # up to ~150s to boot before kubelet gives up
    livenessProbe:
      httpGet: { path: /livez, port: 8080 }   # process-only check, no DB
      periodSeconds: 10
      timeoutSeconds: 2
      failureThreshold: 3          # ~30s of hard failure before restart
    readinessProbe:
      httpGet: { path: /readyz, port: 8080 }
      periodSeconds: 5
      timeoutSeconds: 2
      failureThreshold: 2
      successThreshold: 1
```

### Should readiness check the DB?

This is a judgment call, and the common default is wrong more often than people expect.

- If **every** pod shares the same DB, putting the DB in readiness means a DB outage drops **all** pods from the Service. Clients get connection refused / 503 from the ingress instead of a fast, meaningful error from your app. You avoid restarts, but you still have a full outage, and arguably a less debuggable one.
- Readiness is most useful for **per-pod** conditions: still warming up, its own connection pool is exhausted, it is shutting down, a local cache isn't loaded yet. Those are cases where routing to *other* pods actually helps.
- For a shared dependency, the usually better pattern is to stay ready and handle DB failure in the app: short query timeouts, a circuit breaker, a clear 503 with `Retry-After` on endpoints that need the DB, and healthy responses on endpoints that don't.

My recommendation: keep the DB out of liveness entirely. Keep it out of readiness too, unless the failure is likely to be pod-specific (for example, a per-pod connection pool that can get wedged). Alert on DB health through your monitoring (Prometheus/Datadog) rather than through probes.

### Other details worth getting right

- **Graceful shutdown:** on SIGTERM, have `/readyz` start failing and keep serving in-flight requests for a few seconds before exiting, so endpoints update before the process stops. A `preStop` sleep of 5 to 10s is a common workaround for the endpoint propagation lag.
- **Probe cost:** probes run on every pod every `periodSeconds`. A DB query per probe across 50 pods is constant load you don't need.
- **Don't share the endpoint.** One `/health` used for both probes is how this incident usually starts.

## Rolling it out

- **Blast radius:** probe changes trigger a rolling restart of the Deployment. If the new `/livez` endpoint doesn't exist yet in the image, every new pod fails liveness and the rollout stalls. With default `maxUnavailable` the old pods keep serving, but verify that setting.
- **Order:** ship the image with `/livez` and `/readyz` first, then change the probe config (or do both in one release once you've confirmed the endpoints respond).
- **Verify:** `kubectl rollout status deploy/api`, `kubectl describe pod <pod>` (look for `Liveness probe failed` / `Readiness probe failed` events), and `kubectl get endpoints api` to check that pods are registered. Then test it for real in staging: block DB access with a NetworkPolicy or stop the DB, and confirm the pods **don't restart** (`RESTARTS` stays flat in `kubectl get pods`).
- **Rollback:** `kubectl rollout undo deploy/api`, or revert the manifest/Helm values in Git and let ArgoCD/Flux sync if you're on GitOps.

