#!/usr/bin/env python3
"""Runs every PromQL / LogQL / TraceQL query of the lab dashboard against the backends and reports
errors or empty results. Needs port-forwards:  prometheus :9090, loki :3100, tempo :3200
(scripts/lab.sh validate sets them up)."""
import json, re, sys, time, urllib.parse, urllib.request, pathlib

cm = pathlib.Path(__file__).resolve().parent.parent / "grafana" / "dashboard-java-lab.yaml"
raw = cm.read_text().split("java-observability-lab.json: |\n", 1)[1]
dash = json.loads("\n".join(l[4:] for l in raw.splitlines()))
svc = sys.argv[1] if len(sys.argv) > 1 else "petclinic"

def sub(q):
    return (q.replace("$service", svc).replace("$__rate_interval", "2m").replace("$__auto", "1m")
             .replace("$log_filter", ""))

def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)

bad = 0
now = int(time.time())
for p in dash["panels"]:
    for t in p.get("targets", []):
        ds = t["datasource"]["type"]
        try:
            if ds == "prometheus":
                d = get("http://localhost:9090/api/v1/query?" + urllib.parse.urlencode({"query": sub(t["expr"])}))
                n = len(d["data"]["result"])
            elif ds == "loki":
                d = get("http://localhost:3100/loki/api/v1/query_range?" + urllib.parse.urlencode(
                    {"query": sub(t["expr"]), "start": now - 1800, "end": now, "limit": 5}))
                n = len(d["data"]["result"])
            elif ds == "tempo" and t.get("queryType") == "traceql":
                d = get("http://localhost:3200/api/search?" + urllib.parse.urlencode(
                    {"q": sub(t["query"]), "start": now - 1800, "end": now, "limit": 5}))
                n = len(d.get("traces", []))
            else:
                continue
            status = "ok   " if n else "EMPTY"
        except Exception as e:  # noqa: BLE001
            status, n = "ERROR", str(e)[:150]
            bad += 1
        print(f"{status} {str(n):>4}  {p['title'][:78]}")
sys.exit(1 if bad else 0)
