#!/usr/bin/env bash
# Observability lab control: traffic, chaos scenarios, verification.   ./scripts/lab.sh help
set -euo pipefail
cd "$(dirname "$0")/.."

NS=petclinic
OBS=observability
TOXI="kubectl -n $NS exec deploy/toxiproxy -- /toxiproxy-cli"
PSQL="kubectl -n $NS exec postgres-0 -- psql -U petclinic -d petclinic"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
hint() { printf '\033[0;33m-> %s\033[0m\n' "$*"; }

pf_start() {   # port-forwards for local API calls; cleaned up on exit
  kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090:9090 >/dev/null 2>&1 & PF1=$!
  kubectl -n $OBS port-forward svc/loki 3100:3100 >/dev/null 2>&1 & PF2=$!
  kubectl -n $OBS port-forward svc/tempo 3200:3200 >/dev/null 2>&1 & PF3=$!
  trap 'kill $PF1 $PF2 $PF3 2>/dev/null || true' EXIT
  sleep 3
}

toxic_rm() { $TOXI toxic remove -n "$1" postgres >/dev/null 2>&1 || true; }
proxy_enabled() { $TOXI list | grep -qE '^postgres.*[[:space:]]enabled'; }

case "${1:-help}" in

# ------------------------------------------------------------------ status / verification
status)
  say "Pods";            kubectl get pods -n $OBS -o wide; kubectl get pods -n $NS -o wide
  say "Toxiproxy";       $TOXI inspect postgres || true
  say "Loadgen settings"; kubectl -n $NS get deploy petclinic-loadgen -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name!="JAVA_TOOL_OPTIONS")]}{.name}={.value}{"\n"}{end}' | grep -E '^(RPS|ERROR_PCT|CONCURRENCY)='
  say "Entry points"
  echo "Grafana   : http://$(kubectl -n monitoring get svc monitoring-grafana -o jsonpath='{.status.loadBalancer.ingress[0].ip}')/d/java-observability-lab"
  echo "PetClinic : http://$(kubectl -n $NS get svc petclinic -o jsonpath='{.status.loadBalancer.ingress[0].ip}')/"
  ;;

verify)
  pf_start
  say "Prometheus targets for the lab"
  curl -s localhost:9090/api/v1/targets?state=active | python3 -c "import sys,json;[print(f\"{t['health']:5} {t['scrapeUrl']:45} {t['labels'].get('telemetry_source','')}\") for t in json.load(sys.stdin)['data']['activeTargets'] if 'otel' in t['scrapeUrl'] or t['labels'].get('telemetry_source')]"
  say "Metrics by source (series count)"
  for q in 'count(jvm_memory_used_bytes{telemetry_source="otel"})' 'count(traces_span_metrics_calls_total)' 'count(container_memory_working_set_bytes{namespace="petclinic"})' 'count(kube_pod_info{namespace="petclinic"})'; do
    printf '%-70s %s\n' "$q" "$(curl -s -G localhost:9090/api/v1/query --data-urlencode "query=$q" | python3 -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else 0)')"
  done
  say "Loki streams (last 15m)"
  curl -s -G localhost:3100/loki/api/v1/query --data-urlencode 'query=sum by (service_name) (count_over_time({service_namespace="petclinic-lab"}[15m]))' | python3 -c "import sys,json;[print(' OTLP ',x['metric'],x['value'][1]) for x in json.load(sys.stdin)['data']['result']]"
  curl -s -G localhost:3100/loki/api/v1/query --data-urlencode 'query=sum by (namespace) (count_over_time({source="alloy"}[15m]))' | python3 -c "import sys,json;[print(' Alloy',x['metric'],x['value'][1]) for x in json.load(sys.stdin)['data']['result']]"
  say "Tempo (TraceQL, last 15m)"
  for q in '{resource.service.name="petclinic-loadgen"} >> {span.db.system.name="postgresql"}' '{status=error}'; do
    printf '%-80s %s traces\n' "$q" "$(curl -s -G localhost:3200/api/search --data-urlencode "q=$q" --data-urlencode start=$(( $(date +%s) - 900 )) --data-urlencode end=$(date +%s) --data-urlencode limit=50 | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("traces",[])))')"
  done
  say "Dashboard queries"
  python3 scripts/validate-dashboard.py || true
  ;;

trace)     # ./scripts/lab.sh trace <traceId>  - print a trace as an indented span tree
  [ -n "${2:-}" ] || { echo "usage: $0 trace <traceId>"; exit 1; }
  pf_start
  curl -s "localhost:3200/api/v2/traces/$2" | python3 scripts/trace-tree.py
  ;;

validate)  pf_start; python3 scripts/validate-dashboard.py ;;

grafana-reload)   # re-read provisioned datasources without restarting Grafana (needs your admin password)
  G="http://$(kubectl -n monitoring get svc monitoring-grafana -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
  read -r -p "Grafana user [admin]: " GU; GU=${GU:-admin}
  read -r -s -p "Grafana password: " GP; echo
  curl -s -u "$GU:$GP" -X POST "$G/api/admin/provisioning/datasources/reload"; echo
  curl -s -u "$GU:$GP" "$G/api/datasources" | python3 -c "import sys,json;[print(' ',d['uid'],d['type'],d['url']) for d in json.load(sys.stdin)]"
  ;;

# ------------------------------------------------------------------ traffic
traffic)   # ./scripts/lab.sh traffic <rps> <error_pct>
  kubectl -n $NS set env deploy/petclinic-loadgen RPS="${2:-4}" ERROR_PCT="${3:-5}"
  hint "loadgen restarts with RPS=${2:-4} ERROR_PCT=${3:-5}"
  ;;

burst-errors)   # 60 s of hand-made failing requests, independent of the loadgen
  IP=$(kubectl -n $NS get svc petclinic -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
  say "Sending /oups + unknown-owner requests for 60 s to http://$IP"
  end=$(( $(date +%s) + 60 ))
  while [ "$(date +%s)" -lt $end ]; do
    curl -s -o /dev/null -w '%{http_code} ' -H 'X-Lab-Scenario: manual-burst' "http://$IP/oups"
    curl -s -o /dev/null -w '%{http_code} ' -H 'X-Lab-Scenario: manual-burst' "http://$IP/owners/$RANDOM$RANDOM"
    sleep 0.3
  done; echo
  hint "Dashboard: '5xx error ratio', 'Exceptions by type'. Tempo: { span.http.request.header.x-lab-scenario = \"manual-burst\" }"
  ;;

# ------------------------------------------------------------------ database chaos (via toxiproxy)
db-latency)   # ./scripts/lab.sh db-latency [ms] [jitter_ms]  - every DB round trip gets slower
  toxic_rm db_latency
  $TOXI toxic add -t latency -a latency="${2:-300}" -a jitter="${3:-100}" -n db_latency postgres
  hint "Every JDBC round trip +${2:-300}±${3:-100} ms. A page issues 3-7 queries -> watch P95/P99, 'Slow requests', 'DB query P95'."
  hint "Then: Tempo 'Slow traces' -> open one -> the time sits in the JDBC CLIENT spans. Undo: $0 db-heal"
  ;;

db-timeout)   # connections stall; Hikari gives up after 5 s -> exceptions
  toxic_rm db_timeout
  $TOXI toxic add -t timeout -a timeout=0 -n db_timeout postgres
  hint "Existing connections hang, new ones never complete. Expect 5 s requests, then 500s with"
  hint "SQLTransientConnectionException / CannotCreateTransactionException in traces AND ERROR logs. Undo: $0 db-heal"
  ;;

db-down)      # proxy disabled = connection refused (like the DB being unreachable)
  proxy_enabled && $TOXI toggle postgres
  hint "DB unreachable. Expect 500s (after the 5 s Hikari connection timeout), DB error metrics, ERROR logs. Undo: $0 db-heal"
  ;;

db-lock)      # ./scripts/lab.sh db-lock [seconds] - a real lock inside PostgreSQL, not the network
  S="${2:-20}"
  say "Holding ACCESS EXCLUSIVE lock on table owners for ${S}s"
  hint "Owner pages block -> Hikari pool fills (used==max, pending>0) -> requests queue. Visits/vets pages keep working."
  $PSQL -c "BEGIN; LOCK TABLE owners IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep($S); COMMIT;"
  hint "Postgres logged every statement >200 ms: Loki {namespace=\"petclinic\", container=\"postgres\"} |= \"duration\""
  ;;

db-heal)
  for t in db_latency db_timeout; do toxic_rm $t; done
  proxy_enabled || $TOXI toggle postgres
  $TOXI inspect postgres
  ;;

db-top)       # the slowest statements according to PostgreSQL itself (pg_stat_statements)
  $PSQL -q -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" >/dev/null
  $PSQL -c "SELECT calls, round(mean_exec_time::numeric,2) AS mean_ms, round(max_exec_time::numeric,2) AS max_ms, left(query,90) AS query FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 10;"
  hint "Note: toxiproxy latency is *network* time - invisible here, visible in JDBC spans. db-lock time shows up here too."
  ;;

# ------------------------------------------------------------------ JVM / Kubernetes chaos
pool-starve)  # tiny connection pool + DB latency = queueing inside the app
  kubectl -n $NS set env deploy/petclinic SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE=2
  kubectl -n $NS rollout status deploy/petclinic --timeout=300s
  "$0" db-latency 150 50
  hint "HikariCP panel: used==max(2), pending>0, 'wait for connection' rises. The trace shows gaps before the JDBC spans. Undo: $0 reset"
  ;;

cpu-throttle) # CPU limit 250m: JVM + Tomcat starve, GC and requests slow down
  kubectl -n $NS set resources deploy/petclinic -c petclinic --limits=cpu=250m
  hint "Watch 'CPU throttling' (cAdvisor), JVM CPU (agent) and latency rise together. Undo: $0 reset"
  ;;

oom)          # memory limit below what JVM + agent need -> OOMKilled, restarts
  kubectl -n $NS set resources deploy/petclinic -c petclinic --limits=memory=300Mi --requests=memory=300Mi
  hint "Expect OOMKilled + CrashLoopBackOff: 'Container restarts', 'last terminated reason', pod memory vs limit,"
  hint "and a gap in every OTel metric (the process is gone). kubectl -n $NS describe pod -l app.kubernetes.io/name=petclinic. Undo: $0 reset"
  ;;

reset)        # back to the manifests in git + no toxics + default traffic
  "$0" db-heal || true
  kubectl apply -f apps/petclinic.yaml
  kubectl -n $NS set env deploy/petclinic-loadgen RPS=4 ERROR_PCT=5
  kubectl -n $NS rollout status deploy/petclinic --timeout=300s
  ;;

*)
  sed -n '1,2p' "$0"
  cat <<EOF

verification         status | verify | validate | trace <traceId> | grafana-reload
traffic              traffic <rps> <error_pct> | burst-errors
database chaos       db-latency [ms] [jitter] | db-timeout | db-down | db-lock [sec] | db-top | db-heal
JVM / k8s chaos      pool-starve | cpu-throttle | oom
undo everything      reset
EOF
  ;;
esac
