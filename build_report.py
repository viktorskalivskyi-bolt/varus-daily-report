# -*- coding: utf-8 -*-
"""Operational Report generator (multi-partner).

For each partner in PARTNERS, queries Databricks (SQL Statement Execution API)
and builds a payload: business metrics (GMV / orders / AOV), store coverage,
FAILED block (lost GMV by fault, NDR vs peer benchmark, reasons, hourly,
per-store drill-down) and CS block (tickets, contact rate, escalations).
All payloads are embedded into one self-contained docs/index.html with a
partner selector.

Env vars required:
  DATABRICKS_HOST          e.g. https://bolt-data.cloud.databricks.com
  DATABRICKS_TOKEN         PAT or OAuth access token
  DATABRICKS_WAREHOUSE_ID  SQL warehouse id
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta

HOST = os.environ["DATABRICKS_HOST"].rstrip("/")
TOKEN = os.environ["DATABRICKS_TOKEN"]
WAREHOUSE = os.environ["DATABRICKS_WAREHOUSE_ID"]

BASE = os.path.dirname(os.path.abspath(__file__))
START_DATE = "2026-05-01"

# ---- Partners: add an entry to get the same report for another chain.
PARTNERS = [
    {
        "slug": "varus",
        "title": "VARUS",
        "like": "%varus%",
        "benchmark_vertical": "store_3p_ent",
        "benchmark_label": "Stores 3P Enterprise (peer-мережі UA)",
        "complaints_file": "data/complaints.json",
        "themes": [
            {"name": "Відмова збирати / брак персоналу", "n": 28},
            {"name": "Видалення чи заміна позицій без узгодження", "n": 22},
            {"name": "Готовність / pickup code / видача", "n": 11},
            {"name": "Довге очікування кур'єра", "n": 6},
            {"name": "Графік роботи, вечірні відмови", "n": 5},
            {"name": "Ціни змінені без попередження", "n": 4},
            {"name": "Інше", "n": 3},
        ],
    },
]

LOST_BUCKETS = """
  ROUND(SUM(CASE WHEN order_state = 'failed' AND bad_order_actor_at_fault = 'provider'
        THEN COALESCE(order_gmv_local, 0) ELSE 0 END), 0) AS lost_provider,
  ROUND(SUM(CASE WHEN order_state = 'failed' AND bad_order_actor_at_fault IN ('courier', 'supply')
        THEN COALESCE(order_gmv_local, 0) ELSE 0 END), 0) AS lost_ops,
  ROUND(SUM(CASE WHEN order_state = 'failed'
        AND COALESCE(bad_order_actor_at_fault, '') NOT IN ('provider', 'courier', 'supply')
        THEN COALESCE(order_gmv_local, 0) ELSE 0 END), 0) AS lost_other
"""


def sql(statement: str):
    payload = json.dumps({
        "statement": statement,
        "warehouse_id": WAREHOUSE,
        "wait_timeout": "50s",
        "disposition": "INLINE",
        "format": "JSON_ARRAY",
    }).encode()
    req = urllib.request.Request(
        f"{HOST}/api/2.0/sql/statements",
        data=payload,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    while data["status"]["state"] in ("PENDING", "RUNNING"):
        time.sleep(3)
        req = urllib.request.Request(
            f"{HOST}/api/2.0/sql/statements/{data['statement_id']}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
    if data["status"]["state"] != "SUCCEEDED":
        raise RuntimeError(f"Query failed: {json.dumps(data['status'])[:500]}")
    cols = [c["name"] for c in data["manifest"]["schema"]["columns"]]
    rows = data.get("result", {}).get("data_array", []) or []
    return [dict(zip(cols, r)) for r in rows]


def num(v, default=0):
    if v is None or v == "":
        return default
    f = float(v)
    return int(f) if f == int(f) else f


def build_payload(p):
    F = (
        "country_code = 'ua' AND lower(provider_name) LIKE '{like}' "
        f"AND order_created_date_local >= '{START_DATE}'"
    ).format(like=p["like"])

    q = {}
    for grain, trunc, fmt in (("weekly", "week", "yyyy-MM-dd"), ("monthly", "month", "yyyy-MM")):
        q[grain] = sql(f"""
            SELECT date_format(date_trunc('{trunc}', order_created_date_local), '{fmt}') AS period,
                   COUNT(*) AS orders,
                   SUM(CASE WHEN order_state = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN order_state = 'delivered' THEN 1 ELSE 0 END) AS delivered,
                   ROUND(SUM(CASE WHEN order_state = 'delivered' THEN COALESCE(order_gmv_local, 0) ELSE 0 END), 0) AS gmv_delivered,
                   {LOST_BUCKETS}
            FROM main.ng_delivery.dim_order_delivery
            WHERE {F}
            GROUP BY 1 ORDER BY 1
        """)
        q[f"bench_{grain}"] = sql(f"""
            SELECT date_format(date_trunc('{trunc}', order_created_date_local), '{fmt}') AS period,
                   COUNT(*) AS orders,
                   SUM(CASE WHEN order_state = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM main.ng_delivery.dim_order_delivery
            WHERE country_code = 'ua' AND delivery_vertical = '{p["benchmark_vertical"]}'
              AND order_created_date_local >= '{START_DATE}'
            GROUP BY 1 ORDER BY 1
        """)
        q[f"tickets_{grain}"] = sql(f"""
            WITH o AS (SELECT order_id FROM main.ng_delivery.dim_order_delivery WHERE {F})
            SELECT date_format(date_trunc('{trunc}', sc.created), '{fmt}') AS period,
                   COUNT(*) AS tickets
            FROM main.ng_customer_support.customer_support_support_case sc
            JOIN o ON CAST(sc.order_id AS STRING) = CAST(o.order_id AS STRING)
            WHERE sc.created >= '{START_DATE}'
            GROUP BY 1 ORDER BY 1
        """)

    q["reasons"] = sql(f"""
        SELECT COALESCE(NULLIF(manually_failed_order_reason, ''),
                        NULLIF(failed_order_parent_reason, ''), 'Unknown') AS reason,
               COUNT(*) AS n,
               ROUND(SUM(COALESCE(order_gmv_local, 0)), 0) AS lost_gmv
        FROM main.ng_delivery.dim_order_delivery
        WHERE {F} AND order_state = 'failed'
        GROUP BY 1 ORDER BY lost_gmv DESC LIMIT 12
    """)

    q["hourly"] = sql(f"""
        SELECT hour(order_created_ts_local) AS hr,
               COUNT(*) AS failed,
               ROUND(SUM(COALESCE(order_gmv_local, 0)), 0) AS lost_gmv
        FROM main.ng_delivery.dim_order_delivery
        WHERE {F} AND order_state = 'failed'
        GROUP BY 1 ORDER BY 1
    """)

    q["stores"] = sql(f"""
        SELECT provider_id, provider_name, city_name,
               COUNT(*) AS orders,
               SUM(CASE WHEN order_state = 'failed' THEN 1 ELSE 0 END) AS failed,
               ROUND(100.0 * SUM(CASE WHEN order_state = 'failed' THEN 1 ELSE 0 END) / COUNT(*), 2) AS ndr_pct,
               ROUND(SUM(CASE WHEN order_state = 'failed' THEN COALESCE(order_gmv_local, 0) ELSE 0 END), 0) AS lost_gmv,
               ROUND(SUM(CASE WHEN order_state = 'failed' AND bad_order_actor_at_fault = 'provider'
                     THEN COALESCE(order_gmv_local, 0) ELSE 0 END), 0) AS lost_provider,
               ROUND(SUM(CASE WHEN order_state = 'delivered' THEN COALESCE(order_gmv_local, 0) ELSE 0 END), 0) AS gmv,
               MAX(order_created_date_local) AS last_order
        FROM main.ng_delivery.dim_order_delivery
        WHERE {F}
        GROUP BY 1, 2, 3
        ORDER BY lost_gmv DESC
    """)

    q["store_tickets"] = sql(f"""
        WITH o AS (SELECT order_id, provider_id FROM main.ng_delivery.dim_order_delivery WHERE {F})
        SELECT o.provider_id, COUNT(*) AS tickets
        FROM main.ng_customer_support.customer_support_support_case sc
        JOIN o ON CAST(sc.order_id AS STRING) = CAST(o.order_id AS STRING)
        WHERE sc.created >= '{START_DATE}'
        GROUP BY 1
    """)

    q["store_weekly"] = sql(f"""
        SELECT provider_id,
               date_format(date_trunc('week', order_created_date_local), 'yyyy-MM-dd') AS period,
               SUM(CASE WHEN order_state = 'failed' THEN 1 ELSE 0 END) AS failed,
               ROUND(SUM(CASE WHEN order_state = 'failed' THEN COALESCE(order_gmv_local, 0) ELSE 0 END), 0) AS lost_gmv
        FROM main.ng_delivery.dim_order_delivery
        WHERE {F}
        GROUP BY 1, 2 ORDER BY 1, 2
    """)

    q["cities"] = sql(f"""
        SELECT city_name,
               COUNT(DISTINCT provider_id) AS stores,
               COUNT(*) AS orders,
               ROUND(SUM(CASE WHEN order_state = 'delivered' THEN COALESCE(order_gmv_local, 0) ELSE 0 END), 0) AS gmv,
               SUM(CASE WHEN order_state = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM main.ng_delivery.dim_order_delivery
        WHERE {F}
        GROUP BY 1 ORDER BY orders DESC
    """)

    # ---- assemble
    def series(grain):
        bench = {r["period"]: r for r in q[f"bench_{grain}"]}
        tickets = {r["period"]: num(r["tickets"]) for r in q[f"tickets_{grain}"]}
        out = []
        for r in q[grain]:
            per = r["period"]
            orders, failed, delivered = num(r["orders"]), num(r["failed"]), num(r["delivered"])
            gmv = num(r["gmv_delivered"])
            b = bench.get(per)
            out.append({
                "period": per,
                "orders": orders,
                "failed": failed,
                "gmv": gmv,
                "aov": round(gmv / delivered) if delivered else 0,
                "ndr_pct": round(100.0 * failed / orders, 2) if orders else 0,
                "bench_ndr_pct": round(100.0 * num(b["failed"]) / num(b["orders"]), 2) if b and num(b["orders"]) else None,
                "lost_provider": num(r["lost_provider"]),
                "lost_ops": num(r["lost_ops"]),
                "lost_other": num(r["lost_other"]),
                "tickets": tickets.get(per, 0),
                "contact_rate": round(100.0 * tickets.get(per, 0) / orders, 1) if orders else 0,
            })
        return out

    weekly, monthly = series("weekly"), series("monthly")

    st_tickets = {r["provider_id"]: num(r["tickets"]) for r in q["store_tickets"]}
    stores = [{
        "id": r["provider_id"],
        "name": r["provider_name"].replace(f'{p["title"]}, ', ""),
        "city": r["city_name"],
        "orders": num(r["orders"]),
        "failed": num(r["failed"]),
        "ndr_pct": num(r["ndr_pct"]),
        "lost_gmv": num(r["lost_gmv"]),
        "lost_provider": num(r["lost_provider"]),
        "gmv": num(r["gmv"]),
        "last_order": r["last_order"],
        "tickets": st_tickets.get(r["provider_id"], 0),
    } for r in q["stores"]]

    store_weekly = {}
    for r in q["store_weekly"]:
        store_weekly.setdefault(r["provider_id"], []).append(
            {"period": r["period"], "failed": num(r["failed"]), "lost_gmv": num(r["lost_gmv"])})

    cities = [{
        "city": r["city_name"],
        "stores": num(r["stores"]),
        "orders": num(r["orders"]),
        "gmv": num(r["gmv"]),
        "failed": num(r["failed"]),
    } for r in q["cities"]]

    complaints = []
    if p.get("complaints_file"):
        with open(os.path.join(BASE, p["complaints_file"]), encoding="utf-8") as f:
            complaints = json.load(f)

    kyiv_today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()
    active_cutoff = (kyiv_today - timedelta(days=7)).isoformat()
    active_7d = sum(1 for s in stores if s["last_order"] >= active_cutoff)

    delivered_total = sum(num(r["delivered"]) for r in q["monthly"])
    totals = {
        "orders": sum(r["orders"] for r in monthly),
        "failed": sum(r["failed"] for r in monthly),
        "gmv": sum(r["gmv"] for r in monthly),
        "aov": round(sum(r["gmv"] for r in monthly) / delivered_total) if delivered_total else 0,
        "lost_gmv": sum(r["lost_provider"] + r["lost_ops"] + r["lost_other"] for r in monthly),
        "lost_provider": sum(r["lost_provider"] for r in monthly),
        "tickets": sum(r["tickets"] for r in monthly),
        "stores_total": len(stores),
        "cities_total": len(cities),
        "stores_active_7d": active_7d,
    }
    totals["ndr_pct"] = round(100.0 * totals["failed"] / totals["orders"], 2) if totals["orders"] else 0
    totals["contact_rate"] = round(100.0 * totals["tickets"] / totals["orders"], 1) if totals["orders"] else 0
    b_orders = sum(num(r["orders"]) for r in q["bench_monthly"])
    b_failed = sum(num(r["failed"]) for r in q["bench_monthly"])
    totals["bench_ndr_pct"] = round(100.0 * b_failed / b_orders, 2) if b_orders else 0

    return {
        "title": p["title"],
        "benchmark_label": p["benchmark_label"],
        "totals": totals,
        "weekly": weekly,
        "monthly": monthly,
        "reasons": [{"reason": r["reason"], "n": num(r["n"]), "lost_gmv": num(r["lost_gmv"])} for r in q["reasons"]],
        "hourly": [{"hr": num(r["hr"]), "failed": num(r["failed"]), "lost_gmv": num(r["lost_gmv"])} for r in q["hourly"]],
        "stores": stores,
        "store_weekly": store_weekly,
        "cities": cities,
        "themes": p.get("themes", []),
        "complaints": complaints,
        "active_cutoff": active_cutoff,
    }


def build():
    kyiv_now = datetime.now(timezone.utc) + timedelta(hours=3)
    data_all = {}
    for p in PARTNERS:
        print(f"building: {p['title']} ...")
        data_all[p["slug"]] = build_payload(p)

    meta = {
        "generated_at": kyiv_now.strftime("%d.%m.%Y %H:%M") + " (Київ)",
        "start_date": START_DATE,
        "default": PARTNERS[0]["slug"],
        "partners": [{"slug": p["slug"], "title": p["title"]} for p in PARTNERS],
    }

    with open(os.path.join(BASE, "template.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace(
        "/*__DATA__*/",
        "const META = " + json.dumps(meta, ensure_ascii=False) +
        ";\nconst DATA_ALL = " + json.dumps(data_all, ensure_ascii=False) + ";",
    )

    out = os.path.join(BASE, "docs", "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"OK: {out}")
    for slug, d in data_all.items():
        print(f"{slug}: {d['totals']}")


if __name__ == "__main__":
    build()
