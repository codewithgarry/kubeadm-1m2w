#!/usr/bin/env python3
"""Pretty-prints a Tempo trace (JSON from /api/v2/traces/<id> on stdin) as an indented span tree."""
import collections
import json
import sys


def attrs(items):
    return {a["key"]: list(a["value"].values())[0] for a in items or []}


spans = []
for rs in json.load(sys.stdin)["trace"]["resourceSpans"]:
    svc = attrs(rs["resource"]["attributes"]).get("service.name", "?")
    for ss in rs["scopeSpans"]:
        for s in ss["spans"]:
            s["svc"] = svc
            spans.append(s)

ids = {s["spanId"] for s in spans}
kids = collections.defaultdict(list)
for s in spans:
    kids[s.get("parentSpanId") if s.get("parentSpanId") in ids else None].append(s)


def show(s, depth):
    a = attrs(s.get("attributes"))
    ms = (int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"])) / 1e6
    err = "  !ERROR" if s.get("status", {}).get("code") == "STATUS_CODE_ERROR" else ""
    detail = a.get("db.query.text") or a.get("http.route") or a.get("url.full") or ""
    pad = "  " * depth
    print(f"{ms:9.1f} ms  {pad}{s['svc']}: {s['name']}{err}  {str(detail)[:90]}")
    for e in s.get("events", []):
        ea = attrs(e.get("attributes"))
        print(f"{'':13}{pad}  * event {e['name']}: {ea.get('exception.type', '')} {str(ea.get('exception.message', ''))[:80]}")
    for c in sorted(kids[s["spanId"]], key=lambda x: int(x["startTimeUnixNano"])):
        show(c, depth + 1)


for root in kids[None]:
    show(root, 0)
