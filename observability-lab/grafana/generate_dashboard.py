#!/usr/bin/env python3
"""Generates grafana/dashboard-java-lab.yaml - a ConfigMap picked up by Grafana's dashboard sidecar
(label grafana_dashboard=1, any namespace).

    python3 grafana/generate_dashboard.py && kubectl apply -f grafana/dashboard-java-lab.yaml

Every panel title starts with the data source path so you always know where a number comes from:
    [OTel agent]         Java agent metric  -> OTLP -> Collector -> Prometheus exporter -> Prometheus
    [OTel spanmetrics]   derived from spans by the Collector's span_metrics connector -> Prometheus
    [Prom cAdvisor]      kubelet/cAdvisor container metrics scraped by your existing Prometheus
    [Prom KSM]           kube-state-metrics (object state) scraped by your existing Prometheus
    [Loki OTLP]          application logs pushed by the Java agent (with trace_id / span_id)
    [Loki Alloy]         container stdout collected by Grafana Alloy
    [Tempo]              traces
"""
import json
import pathlib

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}

SVC = 'service_name="$service"'
HTTP = f'{SVC}, http_route!~"/actuator.*"'  # agent HTTP metrics still include kubelet probes
RI = "$__rate_interval"
NS = 'namespace="petclinic"'

panels = []
_id = 0
_y = 0


def nid():
    global _id
    _id += 1
    return _id


def row(title):
    global _y
    panels.append({"type": "row", "title": title, "id": nid(), "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": _y}})
    _y += 1


class Line:
    """Lays panels out left-to-right; a new Line starts below the tallest panel of the previous one."""

    def __init__(self, h):
        global _y
        self.h, self.x, self.y = h, 0, _y
        _y += h

    def place(self, w):
        pos = {"h": self.h, "w": w, "x": self.x, "y": self.y}
        self.x += w
        return pos


def prom(expr, legend="", instant=False, fmt=None):
    t = {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": "A"}
    if instant:
        t.update(instant=True, range=False)
    if fmt:
        t["format"] = fmt
    return t


def targets(*ts):
    for i, t in enumerate(ts):
        t["refId"] = chr(65 + i)
    return list(ts)


def timeseries(line, w, title, tgts, unit="short", desc="", stack=False, overrides=None, min0=True, ds=PROM):
    p = {"type": "timeseries", "title": title, "id": nid(), "gridPos": line.place(w), "datasource": ds,
         "description": desc, "targets": tgts,
         "fieldConfig": {"defaults": {"unit": unit, "custom": {
             "lineWidth": 1, "fillOpacity": 18 if stack else 8, "showPoints": "never",
             "stacking": {"mode": "normal" if stack else "none"}}},
             "overrides": overrides or []},
         "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["mean", "max", "lastNotNull"]},
                     "tooltip": {"mode": "multi", "sort": "desc"}}}
    if min0:
        p["fieldConfig"]["defaults"]["min"] = 0
    panels.append(p)
    return p


def stat(line, w, title, tgt, unit="short", thresholds=None, desc="", decimals=None):
    steps = [{"color": "green", "value": None}] + [{"color": c, "value": v} for v, c in (thresholds or [])]
    p = {"type": "stat", "title": title, "id": nid(), "gridPos": line.place(w), "datasource": PROM,
         "description": desc, "targets": [tgt],
         "fieldConfig": {"defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps},
                                      "color": {"mode": "thresholds"}}, "overrides": []},
         "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "area"}}
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    panels.append(p)


def table(line, w, title, tgts, desc="", ds=PROM, transformations=None, overrides=None):
    panels.append({"type": "table", "title": title, "id": nid(), "gridPos": line.place(w), "datasource": ds,
                   "description": desc, "targets": tgts, "transformations": transformations or [],
                   "fieldConfig": {"defaults": {}, "overrides": overrides or []},
                   "options": {"showHeader": True, "cellHeight": "sm"}})


def logs(line, w, title, expr, desc=""):
    panels.append({"type": "logs", "title": title, "id": nid(), "gridPos": line.place(w), "datasource": LOKI,
                   "description": desc,
                   "targets": [{"datasource": LOKI, "expr": expr, "queryType": "range", "refId": "A"}],
                   "options": {"showTime": True, "wrapLogMessage": True, "enableLogDetails": True,
                               "sortOrder": "Descending", "dedupStrategy": "none", "prettifyLogMessage": False}})


def traceql(line, w, title, q, desc="", limit=20):
    panels.append({"type": "table", "title": title, "id": nid(), "gridPos": line.place(w), "datasource": TEMPO,
                   "description": desc,
                   "targets": [{"datasource": TEMPO, "queryType": "traceql", "query": q, "limit": limit,
                                "spss": 5, "tableType": "traces", "refId": "A"}],
                   "fieldConfig": {"defaults": {}, "overrides": []}, "options": {"showHeader": True}})


def text(line, w, content):
    panels.append({"type": "text", "title": "", "id": nid(), "gridPos": line.place(w),
                   "options": {"mode": "markdown", "content": content}})


def color(name, c):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}


def color_regex(rx, c):
    return {"matcher": {"id": "byRegexp", "options": rx},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}


# ============================================================================ header
l = Line(5)
text(l, 24, """**Where does each number come from?** Every title starts with its path:
`[OTel agent]` Java agent → OTLP → Collector → Prometheus &nbsp;·&nbsp; `[OTel spanmetrics]` computed from spans by the Collector &nbsp;·&nbsp;
`[Prom cAdvisor]` / `[Prom KSM]` Kubernetes metrics scraped by the existing Prometheus &nbsp;·&nbsp; `[Loki OTLP]` app logs with trace_id &nbsp;·&nbsp; `[Loki Alloy]` container stdout &nbsp;·&nbsp; `[Tempo]` traces

**Workflow:** spot it in a metric → click a point / open *Explore* → error or slow **trace** (bottom rows) → open the span → **Logs for this span** (trace_id) → exception stack trace. Chaos: `scripts/lab.sh help`""")

# ============================================================================ golden signals
row("Golden signals — $service")
l = Line(4)
stat(l, 4, "[OTel agent] Requests / s",
     prom(f'sum(rate(http_server_request_duration_seconds_count{{{HTTP}}}[{RI}]))'), "reqps", decimals=1)
stat(l, 4, "[OTel agent] 5xx error ratio",
     prom(f'(sum(rate(http_server_request_duration_seconds_count{{{HTTP}, http_response_status_code=~"5.."}}[{RI}])) or vector(0))'
          f' / sum(rate(http_server_request_duration_seconds_count{{{HTTP}}}[{RI}]))'),
     "percentunit", [(0.02, "orange"), (0.1, "red")], decimals=1)
stat(l, 4, "[OTel agent] P95 latency",
     prom(f'histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{HTTP}}}[{RI}])))'),
     "s", [(0.25, "orange"), (1, "red")])
stat(l, 4, "[OTel spanmetrics] Exceptions / min",
     prom(f'(sum(rate(traces_span_metrics_events_total{{{SVC}, exception_type!=""}}[{RI}])) or vector(0)) * 60'),
     "short", [(1, "orange"), (10, "red")], decimals=1)
stat(l, 4, "[OTel agent] Heap used / max",
     prom(f'sum(jvm_memory_used_bytes{{{SVC}, jvm_memory_type="heap"}}) / sum(jvm_memory_limit_bytes{{{SVC}, jvm_memory_type="heap"}})'),
     "percentunit", [(0.75, "orange"), (0.9, "red")], decimals=0)
stat(l, 4, "[Prom KSM] Container restarts (24h)",
     prom(f'sum(increase(kube_pod_container_status_restarts_total{{{NS}}}[24h])) or vector(0)'),
     "short", [(1, "orange"), (3, "red")], decimals=0)

# ============================================================================ HTTP
row("HTTP server (PetClinic) & client (loadgen)")
l = Line(8)
timeseries(l, 12, "[OTel agent] Request rate by route & status", targets(
    prom(f'sum by (http_request_method, http_route, http_response_status_code) (rate(http_server_request_duration_seconds_count{{{HTTP}}}[{RI}]))',
         "{{http_request_method}} {{http_route}} {{http_response_status_code}}")), "reqps",
    desc="http.server.request.duration histogram count. Route is the low-cardinality template (/owners/{ownerId}), never the raw URL.")
timeseries(l, 12, "[OTel agent] 4xx / 5xx responses", targets(
    prom(f'sum by (http_response_status_code) (rate(http_server_request_duration_seconds_count{{{HTTP}, http_response_status_code=~"4.."}}[{RI}]))',
         "{{http_response_status_code}}"),
    prom(f'sum by (http_response_status_code, error_type) (rate(http_server_request_duration_seconds_count{{{HTTP}, http_response_status_code=~"5.."}}[{RI}]))',
         "{{http_response_status_code}} {{error_type}}")), "reqps", stack=True,
    overrides=[color_regex("4.*", "orange"), color_regex("5.*", "red")],
    desc="error_type on 5xx is the exception class that escaped the controller (OTel semconv).")
l = Line(8)
timeseries(l, 8, "[OTel agent] Latency P50 / P95 / P99", targets(
    *[prom(f'histogram_quantile({q}, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{HTTP}}}[{RI}])))', f"P{int(q * 100)}")
      for q in (0.5, 0.95, 0.99)]), "s",
    overrides=[color("P50", "green"), color("P95", "orange"), color("P99", "red")])
timeseries(l, 8, "[OTel agent] P95 latency by route", targets(
    prom(f'histogram_quantile(0.95, sum by (le, http_route) (rate(http_server_request_duration_seconds_bucket{{{HTTP}}}[{RI}])))',
         "{{http_route}}")), "s")
timeseries(l, 8, "[OTel agent] Slow requests (> 500 ms) / s", targets(
    prom(f'sum by (http_route) (rate(http_server_request_duration_seconds_count{{{HTTP}}}[{RI}]))'
         f' - sum by (http_route) (rate(http_server_request_duration_seconds_bucket{{{HTTP}, le="0.5"}}[{RI}]))',
         "{{http_route}}")), "reqps",
    desc="count - bucket{le=0.5}: requests that took longer than 500 ms. Find the actual traces in the 'Slow traces' table below.")
l = Line(7)
timeseries(l, 12, "[OTel agent] Client vs server view: P95 as seen by loadgen and by PetClinic", targets(
    prom(f'histogram_quantile(0.95, sum by (le) (rate(http_client_request_duration_seconds_bucket{{service_name="petclinic-loadgen"}}[{RI}])))',
         "client (loadgen, http.client.request.duration)"),
    prom(f'histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{HTTP}}}[{RI}])))',
         "server (petclinic, http.server.request.duration)")), "s",
    desc="The gap between the two lines is network + queueing time before Tomcat accepted the request.")
table(l, 12, "[OTel spanmetrics] Slowest operations (P99, current window)", targets(
    prom(f'topk(10, histogram_quantile(0.99, sum by (le, span_name, span_kind) (rate(traces_span_metrics_duration_seconds_bucket{{{SVC}}}[5m]))))',
         instant=True, fmt="table")),
    transformations=[{"id": "organize", "options": {"excludeByName": {"Time": True},
                                                     "renameByName": {"Value": "P99 (s)"}}},
                     {"id": "sortBy", "options": {"sort": [{"field": "P99 (s)", "desc": True}]}}],
    desc="Every span type - HTTP, Spring Data repository, Hibernate, JDBC - ranked by P99.")

# ============================================================================ exceptions
row("Exceptions & error logs")
l = Line(8)
timeseries(l, 12, "[OTel spanmetrics] Exceptions by type & operation", targets(
    prom(f'sum by (exception_type, span_name) (rate(traces_span_metrics_events_total{{{SVC}, exception_type!=""}}[{RI}]))',
         "{{exception_type}} @ {{span_name}}")), "short",
    desc="Counts 'exception' span events recorded by the agent. span_metrics connector, events dimension exception.type.")
timeseries(l, 12, "[Loki OTLP] Log lines by severity", targets(
    {"datasource": LOKI, "expr": f'sum by (severity_text) (count_over_time({{{SVC}}} [$__auto]))',
     "legendFormat": "{{severity_text}}", "queryType": "range", "refId": "A"}), "short", stack=True, ds=LOKI,
    overrides=[color("ERROR", "red"), color("WARN", "orange"), color("INFO", "green")])
l = Line(10)
logs(l, 24, "[Loki OTLP] ERROR logs — expand a line → 'View trace in Tempo' (trace_id)",
     f'{{{SVC}}} | severity_text="ERROR"')

# ============================================================================ database
row("Database — JDBC (PetClinic → toxiproxy → PostgreSQL)")
l = Line(8)
timeseries(l, 8, "[OTel agent] DB query P95 by query summary", targets(
    prom(f'histogram_quantile(0.95, sum by (le, db_query_summary) (rate(db_client_operation_duration_seconds_bucket{{{SVC}}}[{RI}])))',
         "{{db_query_summary}}")), "s",
    desc="db.client.operation.duration (stable DB semconv, OTEL_SEMCONV_STABILITY_OPT_IN=database). db.query.summary = operation + tables.")
timeseries(l, 8, "[OTel agent] DB queries / s by query summary", targets(
    prom(f'sum by (db_query_summary) (rate(db_client_operation_duration_seconds_count{{{SVC}}}[{RI}]))',
         "{{db_query_summary}}")), "ops", stack=True)
timeseries(l, 8, "[OTel agent] DB errors / s", targets(
    prom(f'sum by (error_type) (rate(db_client_operation_duration_seconds_count{{{SVC}, error_type!=""}}[{RI}]))',
         "{{error_type}}")), "ops", overrides=[color_regex(".*", "red")])
l = Line(8)
timeseries(l, 8, "[OTel agent] HikariCP connections", targets(
    prom(f'sum by (db_client_connection_state) (db_client_connection_count{{{SVC}}})', "{{db_client_connection_state}}"),
    prom(f'sum(db_client_connection_max{{{SVC}}})', "max"),
    prom(f'sum(db_client_connection_pending_requests{{{SVC}}})', "pending (threads waiting)")), "short",
    overrides=[color("used", "orange"), color("idle", "green"), color("max", "red"),
               color("pending (threads waiting)", "purple")],
    desc="Pool exhaustion shows as used == max and pending > 0 - every request then waits for a connection.")
timeseries(l, 8, "[OTel agent] Connection wait vs use time (avg)", targets(
    prom(f'sum(rate(db_client_connection_wait_time_seconds_sum{{{SVC}}}[{RI}])) / sum(rate(db_client_connection_wait_time_seconds_count{{{SVC}}}[{RI}]))', "wait for connection"),
    prom(f'sum(rate(db_client_connection_use_time_seconds_sum{{{SVC}}}[{RI}])) / sum(rate(db_client_connection_use_time_seconds_count{{{SVC}}}[{RI}]))', "connection held")), "s")
timeseries(l, 8, "[OTel spanmetrics] JDBC span P95 (client spans with db.system.name)", targets(
    prom(f'histogram_quantile(0.95, sum by (le, span_name) (rate(traces_span_metrics_duration_seconds_bucket{{{SVC}, span_kind="SPAN_KIND_CLIENT", db_system_name="postgresql"}}[{RI}])))',
         "{{span_name}}")), "s",
    desc="Same question, answered from traces instead of the agent's metric - compare with the left-most panel.")

# ============================================================================ JVM
row("JVM — $service")
l = Line(8)
timeseries(l, 8, "[OTel agent] JVM CPU", targets(
    prom(f'sum(jvm_cpu_recent_utilization_ratio{{{SVC}}})', "JVM process (share of available CPUs)"),
    prom(f'sum(jvm_system_cpu_utilization_ratio{{{SVC}}})', "whole node (experimental metric)")), "percentunit",
    desc="jvm.cpu.recent_utilization: 1.0 = all CPUs the JVM sees (container CPU limit rounded up).")
timeseries(l, 8, "[OTel agent] Heap used by pool", targets(
    prom(f'sum by (jvm_memory_pool_name) (jvm_memory_used_bytes{{{SVC}, jvm_memory_type="heap"}})', "{{jvm_memory_pool_name}}"),
    prom(f'sum(jvm_memory_limit_bytes{{{SVC}, jvm_memory_type="heap"}})', "max heap")), "bytes", stack=False,
    overrides=[color("max heap", "red")])
timeseries(l, 8, "[OTel agent] Old gen after last GC (leak indicator)", targets(
    prom(f'sum by (jvm_memory_pool_name) (jvm_memory_used_after_last_gc_bytes{{{SVC}, jvm_memory_type="heap"}})', "{{jvm_memory_pool_name}}")), "bytes",
    desc="If this keeps climbing across GCs, live objects are accumulating.")
l = Line(8)
timeseries(l, 8, "[OTel agent] GC pause time per second", targets(
    prom(f'sum by (jvm_gc_name, jvm_gc_action) (rate(jvm_gc_duration_seconds_sum{{{SVC}}}[{RI}]))', "{{jvm_gc_name}} ({{jvm_gc_action}})")), "s",
    desc="Seconds spent in GC per wall-clock second (0.05 = 5% of time).")
timeseries(l, 8, "[OTel agent] GC collections per second", targets(
    prom(f'sum by (jvm_gc_name) (rate(jvm_gc_duration_seconds_count{{{SVC}}}[{RI}]))', "{{jvm_gc_name}}")), "ops")
timeseries(l, 8, "[OTel agent] Threads by state", targets(
    prom(f'sum by (jvm_thread_state) (jvm_thread_count{{{SVC}}})', "{{jvm_thread_state}}")), "short", stack=True)
l = Line(7)
timeseries(l, 8, "[OTel agent] Non-heap (metaspace, code cache)", targets(
    prom(f'sum by (jvm_memory_pool_name) (jvm_memory_used_bytes{{{SVC}, jvm_memory_type="non_heap"}})', "{{jvm_memory_pool_name}}")), "bytes", stack=True)
timeseries(l, 8, "[OTel agent] Classes loaded", targets(
    prom(f'sum(jvm_class_count{{{SVC}}})', "loaded now")), "short", min0=False)
timeseries(l, 8, "[OTel agent] Buffer pools (direct / mapped)", targets(
    prom(f'sum by (jvm_buffer_pool_name) (jvm_buffer_memory_used_bytes{{{SVC}}})', "{{jvm_buffer_pool_name}}")), "bytes")

# ============================================================================ Kubernetes
row("Kubernetes — namespace petclinic")
l = Line(8)
timeseries(l, 8, "[Prom cAdvisor] Pod CPU usage vs limit", targets(
    prom(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{NS}, container!="", container!="POD"}}[{RI}]))', "{{pod}}"),
    prom(f'sum by (pod) (kube_pod_container_resource_limits{{{NS}, resource="cpu"}})', "limit {{pod}}")), "short",
    overrides=[{"matcher": {"id": "byRegexp", "options": "limit.*"},
                "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 6]}}]}],
    desc="cores. Dashed = limit from kube-state-metrics.")
timeseries(l, 8, "[Prom cAdvisor] Pod memory (working set) vs limit", targets(
    prom(f'sum by (pod) (container_memory_working_set_bytes{{{NS}, container!="", container!="POD"}})', "{{pod}}"),
    prom(f'sum by (pod) (kube_pod_container_resource_limits{{{NS}, resource="memory"}})', "limit {{pod}}")), "bytes",
    overrides=[{"matcher": {"id": "byRegexp", "options": "limit.*"},
                "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 6]}}]}],
    desc="Working set crossing the limit = OOMKilled. Compare with JVM heap above: container memory = heap + metaspace + threads + agent + native.")
timeseries(l, 8, "[Prom cAdvisor] CPU throttling", targets(
    prom(f'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{NS}, container!=""}}[{RI}])) / sum by (pod) (rate(container_cpu_cfs_periods_total{{{NS}, container!=""}}[{RI}]))',
         "{{pod}}")), "percentunit",
    desc="Share of CFS periods in which the container hit its CPU limit - a classic hidden cause of latency.")
l = Line(7)
timeseries(l, 12, "[Prom KSM] Container restarts", targets(
    prom(f'sum by (pod, container) (increase(kube_pod_container_status_restarts_total{{{NS}}}[10m]))', "{{pod}} / {{container}}")), "short",
    desc="kube_pod_container_status_restarts_total, increase over 10 min.")
table(l, 12, "[Prom KSM] Pods — phase, restarts, last termination reason", targets(
    prom(f'sum by (pod, container) (kube_pod_container_status_restarts_total{{{NS}}})', instant=True, fmt="table"),
    prom(f'max by (pod, container, reason) (kube_pod_container_status_last_terminated_reason{{{NS}}}) == 1', instant=True, fmt="table")),
    transformations=[{"id": "merge", "options": {}},
                     {"id": "organize", "options": {"excludeByName": {"Time": True, "Value #B": True},
                                                    "renameByName": {"Value #A": "restarts", "reason": "last terminated"}}}],
    desc="'OOMKilled' in last terminated = the container exceeded its memory limit.")

# ============================================================================ traces
row("Traces (Tempo) — click a Trace ID")
l = Line(9)
traceql(l, 12, "[Tempo] Recent error traces",
        '{ resource.service.name = "$service" && status = error }',
        desc="TraceQL. Open a trace -> the red span -> Events show the exception + stack trace -> 'Logs for this span'.")
traceql(l, 12, "[Tempo] Slow traces (server span > 500 ms)",
        '{ resource.service.name = "$service" && kind = server && duration > 500ms }',
        desc="Open one and look at which child span (JDBC? pool wait?) consumes the time.")
l = Line(9)
traceql(l, 12, "[Tempo] Traces with a slow DB call (> 200 ms)",
        '{ resource.service.name = "$service" && span.db.system.name = "postgresql" && duration > 200ms }')
panels.append({"type": "nodeGraph", "title": "[OTel spanmetrics → Tempo] Service map", "id": nid(),
               "gridPos": l.place(12), "datasource": TEMPO,
               "description": "Built by Grafana from traces_service_graph_* metrics (Collector service_graph connector).",
               "targets": [{"datasource": TEMPO, "queryType": "serviceMap", "refId": "A",
                            "serviceMapQuery": '{service_namespace="petclinic-lab"}'}]})

# ============================================================================ logs
row("Logs (Loki)")
l = Line(10)
logs(l, 12, "[Loki OTLP] Application logs — petclinic + loadgen (trace-correlated)",
     '{service_namespace="petclinic-lab"} |~ "(?i)$log_filter"')
logs(l, 12, "[Loki Alloy] Kubernetes container logs — namespace petclinic (postgres slow queries, toxiproxy)",
     '{namespace="petclinic", source="alloy"} |~ "(?i)$log_filter"')

dashboard = {
    "uid": "java-observability-lab",
    "title": "Java Observability Lab — PetClinic",
    "tags": ["observability-lab", "java", "opentelemetry"],
    "timezone": "browser",
    "schemaVersion": 41,
    "version": 1,
    "editable": True,
    "graphTooltip": 1,
    "refresh": "30s",
    "time": {"from": "now-30m", "to": "now"},
    "templating": {"list": [
        {"name": "service", "label": "Service", "type": "query", "datasource": PROM,
         "query": {"query": 'label_values(jvm_thread_count{service_namespace="petclinic-lab"}, service_name)', "refId": "q"},
         "definition": 'label_values(jvm_thread_count{service_namespace="petclinic-lab"}, service_name)',
         "current": {"text": "petclinic", "value": "petclinic"}, "refresh": 2, "sort": 1},
        {"name": "log_filter", "label": "Log filter (regex)", "type": "textbox", "query": "",
         "current": {"text": "", "value": ""}},
    ]},
    "links": [
        {"title": "Explore traces", "type": "link", "icon": "external link", "targetBlank": True,
         "url": "/explore?left=%7B%22datasource%22:%22tempo%22%7D"},
        {"title": "Explore logs", "type": "link", "icon": "external link", "targetBlank": True,
         "url": "/explore?left=%7B%22datasource%22:%22loki%22%7D"},
    ],
    "annotations": {"list": [
        {"name": "Pod restarts", "datasource": PROM, "enable": True, "iconColor": "red",
         "expr": f'changes(kube_pod_container_status_restarts_total{{{NS}}}[2m]) > 0',
         "titleFormat": "restart {{pod}}", "step": "60s"},
    ]},
    "panels": panels,
}

out = pathlib.Path(__file__).with_name("dashboard-java-lab.yaml")
body = json.dumps(dashboard, indent=1)
cm = f"""# GENERATED by grafana/generate_dashboard.py - edit that file, then re-run it.
apiVersion: v1
kind: ConfigMap
metadata:
  name: dashboard-java-observability-lab
  namespace: observability
  labels:
    grafana_dashboard: "1"
    app.kubernetes.io/part-of: observability-lab
data:
  java-observability-lab.json: |
""" + "\n".join("    " + ln for ln in body.splitlines()) + "\n"
out.write_text(cm)
print(f"wrote {out} ({len(panels)} panels)")
