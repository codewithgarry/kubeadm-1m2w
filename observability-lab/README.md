# Java Observability Lab — OpenTelemetry · Prometheus · Loki · Tempo · Grafana

A hands-on lab for Java application monitoring on a 3-node kubeadm cluster (arm64). It follows the same idea as Dynatrace-style full-stack monitoring, but uses only open-source parts.
You break things on purpose and investigate them: **Metrics → Traces → Spans → Database → Exception → Logs**.

```
./scripts/install.sh          # install / re-apply everything (idempotent)
./scripts/lab.sh grafana-reload   # once: make Grafana load the Loki/Tempo datasources (see "Grafana note")
./scripts/lab.sh status       # URLs + state
./scripts/lab.sh verify       # end-to-end check of every signal
./scripts/lab.sh help         # chaos scenarios
```

Dashboard: **Grafana → Dashboards → "Java Observability Lab — PetClinic"** (`/d/java-observability-lab`)

---

## 1. Architecture

```
                               namespace: petclinic
 ┌────────────────────────────────────────────────────────────────────────────────────────┐
 │  petclinic-loadgen (Java 21 + OTel agent)                                                │
 │      │ HTTP (W3C traceparent)   ── HTTP CLIENT spans                                     │
 │      ▼                                                                                   │
 │  petclinic (Spring Boot 4.1 on Temurin 21 JRE + OTel Java agent 2.31.1)                  │
 │      │ JDBC                    ── SERVER / Spring Data / Hibernate / JDBC CLIENT spans   │
 │      ▼                                                                                   │
 │  toxiproxy :5432  ──►  postgres:17 (StatefulSet, PVC 2Gi)                                │
 │  (fault injection:        logs statements > 200 ms                                       │
 │   latency/timeout/down)                                                                  │
 └──────────┬──────────────────────────────────────────────────────────────┬──────────────┘
            │ OTLP/HTTP :4318  (traces, metrics, logs)                     │ stdout/stderr
            ▼                                                              │ /var/log/pods
 ┌────────────────────────── namespace: observability ───────────────────────────────────┐
 │  OpenTelemetry Collector (contrib 0.161.0, Deployment)                                  │
 │   receivers: otlp                                                                       │
 │   processors: memory_limiter → filter(drop /actuator probes) → k8sattributes            │
 │               → transform(log severity) → batch                                         │
 │   connectors: span_metrics (RED + exception counts), service_graph                      │
 │   exporters:  otlp → Tempo      otlphttp → Loki /otlp      prometheus :8889             │
 │        │                 │                    ▲                    │                    │
 │        ▼                 ▼                    │                    │                    │
 │   Tempo 3.0.3        Loki 3.7.8 ◄──── Grafana Alloy (DaemonSet, all 3 nodes)            │
 │   (PVC 5Gi, 72h)     (PVC 5Gi, 72h)    every other container's logs                    │
 └────────┬─────────────────┬────────────────────────────────────────┬───────────────────┘
          │                 │                          ServiceMonitor │ (release=monitoring)
 ┌────────┴─────────────────┴── namespace: monitoring (EXISTING) ─────▼──────────────────┐
 │  Grafana 13.2.2  ◄── datasources: Prometheus (existing), Loki, Tempo (new ConfigMap)  │
 │  Prometheus  ◄── scrapes: kubelet/cAdvisor, kube-state-metrics, node-exporter          │
 │              ◄── NEW: otel-collector :8889 (app metrics) + :8888 (collector health)     │
 └───────────────────────────────────────────────────────────────────────────────────────┘
```

**Correlation paths**

| From | To | How |
|---|---|---|
| Trace / span | Logs | Tempo datasource `tracesToLogsV2`: `{service_namespace="petclinic-lab"} \| trace_id="<id>"` (trace_id is Loki structured metadata written by the OTLP endpoint) |
| Log line | Trace | Loki derived field on the `trace_id` label → "View trace in Tempo" |
| Span | Metrics | Tempo `tracesToMetrics` → span_metrics rate / errors / P95 for that operation |
| Services | Service map | `traces_service_graph_*` metrics (Collector `service_graph` connector) → Tempo "Service Graph" tab / node graph panel |

## 2. What was found in the cluster before installing

| Item | Finding | Consequence |
|---|---|---|
| Nodes | kubeadm v1.37.1, 1 control plane (tainted) + 2 workers, **arm64**, 2 vCPU / 3.7 GiB each | every image must be arm64; small resource budgets |
| Monitoring | Helm release `monitoring` = kube-prometheus-stack 91.5.0 in ns `monitoring` | reused as-is, **not upgraded** |
| Prometheus | `monitoring-kube-prometheus-prometheus:9090`, selectors `release: monitoring`, all namespaces; remote-write receiver + exemplar storage **off** | app metrics enter through a ServiceMonitor (pull), not remote-write |
| Grafana | `monitoring-grafana`, LoadBalancer 192.168.64.201; sidecars: dashboards from **all** namespaces, datasources only from **`monitoring`** | datasource ConfigMap lives in `monitoring`, dashboard ConfigMap in `observability` |
| Grafana storage | **emptyDir** — deleting the pod loses UI-made dashboards, users, password changes | we never restart Grafana |
| Storage | `local-path` (default, WaitForFirstConsumer) | PVCs for Loki, Tempo, Postgres, PetClinic jar |
| Also present | MetalLB (pool 192.168.64.200-220 + 100-250), Traefik, metrics-server, PrestaShop + MySQL in `default` | untouched |

## 3. Components and versions

| Component | Chart / image | Why this one |
|---|---|---|
| OTel Collector | `open-telemetry/opentelemetry-collector` 0.174.0 → `otel/opentelemetry-collector-contrib:0.161.0` | contrib build has k8sattributes, span_metrics, service_graph, filter, transform |
| Loki | `grafana-community/loki` 18.13.7 → Loki 3.7.8, **Monolithic** | Loki/Tempo charts moved to the `grafana-community` repo; the `grafana/` copies are stale. Monolithic = one pod, filesystem on PVC, no memcached |
| Tempo | `grafana-community/tempo` 3.0.0 → Tempo 3.0.3 single binary | local backend on PVC |
| Alloy | `grafana/alloy` 1.13.0 → Alloy v1.20.0 (DaemonSet) | Promtail's successor |
| Java agent | `ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-java:2.31.1` (initContainer copies the jar) | latest agent release, from a registry instead of a runtime download |
| App | **Spring PetClinic**, `spring-projects/spring-petclinic` @ `500158f` (Spring Boot 4.1.0), run on `eclipse-temurin:21-jre` | see below |
| Database | `postgres:17-alpine` StatefulSet | PetClinic's `postgres` profile |
| Fault injection | `ghcr.io/shopify/toxiproxy:2.12.0` | controlled DB latency / timeouts / outages |
| Load generator | `LoadGen.java` on `eclipse-temurin:21-jdk` + OTel agent | produces **HTTP client** spans and cross-service traces |

**Why PetClinic is built in-cluster:** `springcommunity/spring-petclinic:latest` and `3.5.6` are **amd64-only**. The one arm64 tag, `3.5.0`, is a **GraalVM native image**: it has no JVM, so `-javaagent` is ignored and the OTel Java agent can't instrument it. (This was verified on the cluster: startup took 0.26 s and the GC was named "young generation scavenger".) So `apps/petclinic-build.yaml` builds the official source, pinned to a commit, with the official `maven` image, and writes the jar to a PVC. The official Temurin JRE then runs it. There's no custom image and no registry.

## 4. Files

```
00-namespaces.yaml              observability + petclinic namespaces
helm/loki-values.yaml           Loki monolithic, OTLP label config, retention
helm/tempo-values.yaml          Tempo single binary, PVC, retention
helm/otel-collector-values.yaml complete Collector config (pipelines, connectors, ServiceMonitor)
helm/alloy-values.yaml          Alloy DaemonSet + River config
apps/postgres.yaml              Secret, Service, StatefulSet
apps/toxiproxy.yaml             ConfigMap, Service, Deployment
apps/petclinic-build.yaml       PVC + Job (source -> jar)
apps/petclinic.yaml             OTel agent ConfigMap, LoadBalancer Service, Deployment
apps/loadgen/                   LoadGen.java + Deployment (kubectl apply -k)
grafana/datasources.yaml        Loki + Tempo datasources with correlation (-> ns monitoring)
grafana/generate_dashboard.py   dashboard source; writes grafana/dashboard-java-lab.yaml
scripts/install.sh              ordered, validated install
scripts/lab.sh                  status / verify / traffic / chaos / reset
scripts/validate-dashboard.py   runs every dashboard query against the backends
scripts/trace-tree.py           prints a trace as a span tree in the terminal
```

## 5. Java instrumentation (no code changes)

Everything is configured with environment variables (`apps/petclinic.yaml`, ConfigMap `petclinic-otel`):

| Setting | Effect |
|---|---|
| `JAVA_TOOL_OPTIONS=-javaagent:/otel-agent/opentelemetry-javaagent.jar -XX:+UseG1GC ...` | attaches the agent; G1 set explicitly, because a small container would otherwise make the JVM silently pick SerialGC |
| `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | `service.name`, `service.namespace=petclinic-lab`, `service.version`, `deployment.environment.name`, pod IP (for k8sattributes) |
| `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.observability:4318` | traces + metrics + logs over OTLP/HTTP |
| `OTEL_SEMCONV_STABILITY_OPT_IN=database` | stable DB conventions: `db.system.name`, `db.query.text`, `db.query.summary`, **`db.client.operation.duration`** histogram |
| `OTEL_LOGS_EXPORTER=otlp` + logback/JUL instrumentation | every log record exported with `trace_id`/`span_id`, and exceptions as `exception.type/message/stacktrace` |
| `LOGGING_PATTERN_LEVEL=%5p [trace_id=%X{trace_id:-} span_id=%X{span_id:-}]` | trace IDs in `kubectl logs` output too (the agent fills the logback MDC) |
| `OTEL_INSTRUMENTATION_HTTP_SERVER_CAPTURE_REQUEST_HEADERS=x-lab-scenario` | the loadgen's scenario name becomes a span attribute |
| `OTEL_INSTRUMENTATION_COMMON_DB_QUERY_SANITIZATION_ENABLED=true` | bind values replaced by `?` in `db.query.text` |

What you get per request: HTTP server span → Spring MVC → Spring Data repository span → Hibernate session and transaction spans → JDBC client spans with SQL. Exceptions appear as span events with stack traces. The loadgen adds an HTTP client span as the trace root (the `java.net.http.HttpClient` instrumentation).

## 6. Which metric comes from where

`telemetry_source` label on every scraped series of the Collector: `otel` = application, `otel-collector-self` = the Collector's own health.

| Prefix / metric | Produced by | Path into Prometheus |
|---|---|---|
| `jvm_*` (memory, gc, threads, cpu, classes, buffers) | **OTel Java agent** runtime-telemetry | OTLP → Collector → `prometheus` exporter → ServiceMonitor |
| `http_server_request_duration_seconds_*`, `http_client_request_duration_seconds_*` | **OTel Java agent** HTTP instrumentation | same |
| `db_client_operation_duration_seconds_*`, `db_client_connection_*` (HikariCP) | **OTel Java agent** JDBC / Hikari instrumentation | same |
| `traces_span_metrics_*` (calls, duration, **events** = exceptions) | **OTel Collector** `span_metrics` connector, computed from spans | same |
| `traces_service_graph_*` | **OTel Collector** `service_graph` connector | same |
| `otelcol_*` | the Collector itself | ServiceMonitor, port 8888 |
| `container_cpu_*`, `container_memory_*`, `container_cpu_cfs_*` | **Kubernetes** kubelet/cAdvisor | existing Prometheus scrape (kube-prometheus-stack) |
| `kube_pod_*`, `kube_deployment_*` (restarts, OOMKilled, limits) | **Kubernetes** kube-state-metrics | existing Prometheus scrape |
| `node_*` | node-exporter | existing Prometheus scrape |

On app metrics, `k8s_namespace_name`, `k8s_pod_name` and `k8s_node_name` describe **the application pod**. They're added by the k8sattributes processor. The scrape target's own `namespace`/`pod` labels, which would describe the Collector pod, are dropped on purpose.

## 7. Grafana note (one-time)

Grafana's sidecar hot-reloads datasources through Grafana's admin API using the Helm secret's password. In this cluster the admin password was changed in the UI, so that reload returns `401`. The dashboard still loads, because dashboards are polled from files, but **Loki and Tempo only show up after one reload**:

```
./scripts/lab.sh grafana-reload        # prompts for your current Grafana admin password
```

Restarting Grafana would also load them. Don't do it unless you accept losing UI-made changes: Grafana's storage is an emptyDir. It's worth setting `grafana.persistence.enabled=true` in your kube-prometheus-stack values at some point.

## 8. Hands-on exercises

Each exercise: **run the scenario → find it in metrics → drill into a trace → span → DB / exception → log**. Then run `./scripts/lab.sh db-heal` or `./scripts/lab.sh reset`.

### Exercise 1 — Exception hunt (baseline, always on)
The loadgen sends ~5% failing requests (`/oups`, unknown owner IDs, 404s). For more, run `./scripts/lab.sh burst-errors` or `./scripts/lab.sh traffic 6 30`.
1. Dashboard → *5xx error ratio* and *Exceptions by type & operation*. You'll see `java.lang.RuntimeException @ GET /oups` and `IllegalArgumentException @ GET /owners/{ownerId}`.
2. *Recent error traces* → click a Trace ID. The loadgen `GET` (client) → petclinic `GET /owners/{ownerId}` (server, red) → **Events** tab: exception type, message, stack trace.
3. On the red span → **Logs for this span**. Loki shows the ERROR record with the same `trace_id` and `span_id`, plus `exception_type` and the full `exception_stacktrace` in the log details.
4. Reverse direction: *ERROR logs* panel → expand a line → **View trace in Tempo**.

### Exercise 2 — Slow database (network latency)
```
./scripts/lab.sh db-latency 300 100
```
1. Within ~1 min: *P50/P95/P99* climbs from ~10 ms to seconds; *Slow requests (> 500 ms)* lights up for `/owners`; *DB query P95 by query summary* ≈ 0.4 s for every query.
2. *Slow traces* → open one. Three sequential JDBC spans (`select owners` → `select pets types` → `select visits`), ~350 ms each, add up to a 1 s+ request. That's the classic *N sequential round trips* pattern.
3. `./scripts/lab.sh db-top`: PostgreSQL itself says the queries are fast. The latency is in the network, and only the client-side JDBC span shows it. (Lesson: measure on the client.)

### Exercise 3 — Database outage
```
./scripts/lab.sh db-down      # or: db-timeout (connections hang instead of being refused)
```
1. 5xx ratio jumps. *DB errors / s* and *HikariCP connections* (used drops, pending rises) change.
2. Error traces last **~5 s** (the Hikari `connectionTimeout`). Span events: `DataAccessResourceFailureException` → `JDBCConnectionException: Unable to acquire JDBC Connection`. The exception is recorded at every layer: server span, repository span, Hibernate span.
3. Logs: Hikari `WARN Failed to validate connection` and Hibernate `Connection is not available, request timed out after 5000ms`, all carrying trace IDs.

### Exercise 4 — Lock contention inside PostgreSQL
```
./scripts/lab.sh db-lock 20
```
1. Only owner pages hang; `/vets.html` stays fast. Compare *P95 latency by route*.
2. *HikariCP connections*: `used` hits `max` and `pending` > 0. The pool is exhausted by requests waiting on the lock.
3. Logs panel *Kubernetes container logs* (Alloy): Postgres logged `duration: 14372 ms ... select ... from owners`. That's the database's own view of the same event, from a different log pipeline.
4. `./scripts/lab.sh db-top` shows the `pg_sleep` holder.

### Exercise 5 — Connection-pool starvation
```
./scripts/lab.sh pool-starve     # pool size 2 + 150 ms DB latency
```
*Connection wait vs use time*: wait time dominates. Traces show gaps before the JDBC spans (time spent waiting for a connection).

### Exercise 6 — CPU throttling
```
./scripts/lab.sh cpu-throttle    # CPU limit 250m
```
*CPU throttling* (cAdvisor), *JVM CPU* (agent), *GC pause time* and latency rise together. That's the same problem seen from Kubernetes, the JVM and the user.

### Exercise 7 — OOMKilled
```
./scripts/lab.sh oom             # memory limit 300Mi
```
*Container restarts* and *last terminated reason = OOMKilled* (kube-state-metrics) show up in *Pod memory vs limit*. The new pod crash-loops while the old ReplicaSet keeps serving, so you're practicing a stuck rollout. `kubectl -n petclinic describe pod -l app.kubernetes.io/name=petclinic` shows `Last State: Terminated, Reason: OOMKilled, Exit Code: 137`.

Reset everything: `./scripts/lab.sh reset`

### Useful queries

TraceQL (Grafana → Explore → Tempo):
```
{ resource.service.name = "petclinic" && status = error }
{ resource.service.name = "petclinic" && kind = server && duration > 500ms }
{ span.db.system.name = "postgresql" && duration > 200ms }
{ resource.service.name = "petclinic-loadgen" } >> { span.db.system.name = "postgresql" }
{ span.http.response.status_code >= 500 } | by(span.http.route) | count() > 2
```
LogQL (Explore → Loki):
```
{service_name="petclinic"} | severity_text="ERROR"
{service_name="petclinic"} | trace_id="<paste>"
sum by (exception_type) (count_over_time({service_name="petclinic"} | exception_type!="" [5m]))
{namespace="petclinic", container="postgres"} |= "duration"
```
PromQL (Explore → Prometheus):
```
histogram_quantile(0.99, sum by (le, http_route) (rate(http_server_request_duration_seconds_bucket{service_name="petclinic"}[5m])))
sum by (exception_type) (rate(traces_span_metrics_events_total{exception_type!=""}[5m]))
histogram_quantile(0.95, sum by (le, db_query_summary) (rate(db_client_operation_duration_seconds_bucket[5m])))
```

## 9. Verification & troubleshooting

```bash
./scripts/lab.sh verify                                   # targets, series per source, Loki streams, TraceQL, dashboard queries
./scripts/lab.sh trace <traceId>                          # span tree in the terminal

# pipeline health
kubectl -n observability logs deploy/otel-collector | grep -iE "error|warn|dropped"
# accepted vs sent per signal (the collector binds to the pod IP, so use the API-server proxy, not port-forward)
kubectl get --raw /api/v1/namespaces/observability/services/otel-collector:8888/proxy/metrics | grep -E "^otelcol_(receiver_accepted|receiver_refused|exporter_sent|exporter_send_failed)_(spans|log_records|metric_points)"
kubectl -n petclinic logs deploy/petclinic -c petclinic | grep otel.javaagent     # agent version + config warnings
kubectl -n petclinic logs deploy/petclinic -c petclinic | grep -i "OTLP.*fail"   # export failures from the app side

# backends
kubectl -n observability port-forward svc/loki 3100 & curl -s localhost:3100/ready; curl -sG localhost:3100/loki/api/v1/label/service_name/values
kubectl -n observability port-forward svc/tempo 3200 & curl -s localhost:3200/ready; curl -s localhost:3200/status/services
kubectl -n observability logs sts/tempo | grep -E "level=(error|warn)" | grep -v "no jobs found"   # "no jobs found" = idle compactor, harmless
kubectl -n observability logs ds/alloy -c alloy | grep level=error
kubectl -n observability port-forward ds/alloy 12345 & open http://localhost:12345      # Alloy UI: component graph + health

# Prometheus picked up the ServiceMonitor?
kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090 & open "http://localhost:9090/targets?search=otel"

# Grafana datasources
kubectl -n monitoring logs deploy/monitoring-grafana -c grafana-sc-datasources --tail=20
./scripts/lab.sh grafana-reload

# app
kubectl -n petclinic exec deploy/toxiproxy -- /toxiproxy-cli list              # proxy enabled? toxics?
kubectl -n petclinic exec postgres-0 -- psql -U petclinic -c "select count(*) from owners"
```

| Symptom | Likely cause |
|---|---|
| No Loki/Tempo datasource in Grafana | reload not done → `./scripts/lab.sh grafana-reload` |
| "Logs for this span" returns nothing | log arrived via Alloy (no `trace_id` label) instead of OTLP, or the time shift is too small; check `{service_name="petclinic"} \| trace_id="…"` in Explore |
| JVM metrics missing, traces present | `OTEL_METRICS_EXPORTER` / ServiceMonitor label `release: monitoring` / target down on :8889 |
| Loadgen spans missing | the agent refuses JDK tools (`java File.java`) unless `OTEL_JAVAAGENT_ENABLED=true` |
| Petclinic slow to start / CrashLoop | memory limit too low for JVM + agent (~450 MiB working set) or CPU throttled; see Kubernetes row |
| Nodes under memory pressure | lab uses ~1.5 GiB RAM in total; workers were at ~65–72% afterwards. Scale the PrestaShop `nginx-deployment` in `default` down if needed |

## 10. Uninstall

```bash
helm -n observability uninstall otel-collector alloy tempo loki
kubectl delete -f grafana/datasources.yaml -f grafana/dashboard-java-lab.yaml
kubectl delete ns petclinic observability        # also deletes the PVCs (local-path reclaim: Delete)
```
