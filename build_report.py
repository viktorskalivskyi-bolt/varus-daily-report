# -*- coding: utf-8 -*-
"""Partner daily ops report generator (template: works for any provider chain).

Queries Databricks (SQL Statement Execution API) and renders a self-contained
HTML dashboard (docs/index.html) with two main blocks:
  - FAILED: lost GMV split by fault, NDR vs peer benchmark, fail reasons,
    hour-of-day pattern, per-store drill-down;
  - CS: ticket volume, contact rate, curated Slack escalations.

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

# ---- Partner config: to build the same report for another partner, add an
# entry here (slug, display title, provider_name LIKE pattern, peer vertical).
PARTNER = {
    "slug": "varus",
    "title": "VARUS",
    "like": "%varus%",
    "benchmark_vertical": "store_3p_ent",
    "benchmark_label": "Stores 3P Enterprise (peer-мережі UA)",
}

F = (
    "country_code = 'ua' AND lower(provider_name) LIKE '{like}' "
    f"AND order_created_date_local >= '{START_DATE}'"
).format(like=PARTNER["like"])

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


def fetch():
    q = {}

    for grain, trunc, fmt in (("weekly", "week", "yyyy-MM-dd"), ("monthly", "month", "yyyy-MM")):
        q[grain] = sql(f"""
            SELECT date_format(date_trunc('{trunc}', order_created_date_local), '{fmt}') AS period,
                   COUNT(*) AS orders,
                   SUM(CASE WHEN order_state = 'failed' THEN 1 ELSE 0 END) AS failed,
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
            WHERE country_code = 'ua' AND delivery_vertical = '{PARTNER["benchmark_vertical"]}'
              AND order_created_date_local >= '{START_DATE}'
            GROUP BY 1 ORDER BY 1
        """)
        q[f"tickets_{grain}"] = sql(f"""
            WITH p AS (SELECT order_id FROM main.ng_delivery.dim_order_delivery WHERE {F})
            SELECT date_format(date_trunc('{trunc}', sc.created), '{fmt}') AS period,
                   COUNT(*) AS tickets
            FROM main.ng_customer_support.customer_support_support_case sc
            JOIN p ON CAST(sc.order_id AS STRING) = CAST(p.order_id AS STRING)
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
                     THEN COALESCE(order_gmv_local, 0) ELSE 0 END), 0) AS lost_provider
        FROM main.ng_delivery.dim_order_delivery
        WHERE {F}
        GROUP BY 1, 2, 3
        ORDER BY lost_gmv DESC
    """)

    q["store_tickets"] = sql(f"""
        WITH p AS (SELECT order_id, provider_id FROM main.ng_delivery.dim_order_delivery WHERE {F})
        SELECT p.provider_id, COUNT(*) AS tickets
        FROM main.ng_customer_support.customer_support_support_case sc
        JOIN p ON CAST(sc.order_id AS STRING) = CAST(p.order_id AS STRING)
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

    q["gmv_delivered"] = sql(f"""
        SELECT ROUND(SUM(COALESCE(order_gmv_local, 0)), 0) AS gmv
        FROM main.ng_delivery.dim_order_delivery
        WHERE {F} AND order_state = 'delivered'
    """)

    return q


def build():
    q = fetch()

    def series(grain):
        bench = {r["period"]: r for r in q[f"bench_{grain}"]}
        tickets = {r["period"]: num(r["tickets"]) for r in q[f"tickets_{grain}"]}
        out = []
        for r in q[grain]:
            p = r["period"]
            orders, failed = num(r["orders"]), num(r["failed"])
            b = bench.get(p)
            out.append({
                "period": p,
                "orders": orders,
                "failed": failed,
                "ndr_pct": round(100.0 * failed / orders, 2) if orders else 0,
                "bench_ndr_pct": round(100.0 * num(b["failed"]) / num(b["orders"]), 2) if b and num(b["orders"]) else None,
                "lost_provider": num(r["lost_provider"]),
                "lost_ops": num(r["lost_ops"]),
                "lost_other": num(r["lost_other"]),
                "tickets": tickets.get(p, 0),
                "contact_rate": round(100.0 * tickets.get(p, 0) / orders, 1) if orders else 0,
            })
        return out

    weekly, monthly = series("weekly"), series("monthly")

    st_tickets = {r["provider_id"]: num(r["tickets"]) for r in q["store_tickets"]}
    stores = [{
        "id": r["provider_id"],
        "name": r["provider_name"].replace(f'{PARTNER["title"]}, ', ""),
        "city": r["city_name"],
        "orders": num(r["orders"]),
        "failed": num(r["failed"]),
        "ndr_pct": num(r["ndr_pct"]),
        "lost_gmv": num(r["lost_gmv"]),
        "lost_provider": num(r["lost_provider"]),
        "tickets": st_tickets.get(r["provider_id"], 0),
    } for r in q["stores"]]

    store_weekly = {}
    for r in q["store_weekly"]:
        store_weekly.setdefault(r["provider_id"], []).append(
            {"period": r["period"], "failed": num(r["failed"]), "lost_gmv": num(r["lost_gmv"])})

    reasons = [{"reason": r["reason"], "n": num(r["n"]), "lost_gmv": num(r["lost_gmv"])} for r in q["reasons"]]
    hourly = [{"hr": num(r["hr"]), "failed": num(r["failed"]), "lost_gmv": num(r["lost_gmv"])} for r in q["hourly"]]

    with open(os.path.join(BASE, "data", "complaints.json"), encoding="utf-8") as f:
        complaints = json.load(f)

    totals = {
        "orders": sum(r["orders"] for r in monthly),
        "failed": sum(r["failed"] for r in monthly),
        "lost_gmv": sum(r["lost_provider"] + r["lost_ops"] + r["lost_other"] for r in monthly),
        "lost_provider": sum(r["lost_provider"] for r in monthly),
        "tickets": sum(r["tickets"] for r in monthly),
        "gmv_delivered": num(q["gmv_delivered"][0]["gmv"]) if q["gmv_delivered"] else 0,
    }
    totals["ndr_pct"] = round(100.0 * totals["failed"] / totals["orders"], 2) if totals["orders"] else 0
    totals["contact_rate"] = round(100.0 * totals["tickets"] / totals["orders"], 1) if totals["orders"] else 0
    b_orders = sum(num(r["orders"]) for r in q["bench_monthly"])
    b_failed = sum(num(r["failed"]) for r in q["bench_monthly"])
    totals["bench_ndr_pct"] = round(100.0 * b_failed / b_orders, 2) if b_orders else 0

    kyiv_now = datetime.now(timezone.utc) + timedelta(hours=3)
    payload = {
        "partner": {"title": PARTNER["title"], "benchmark_label": PARTNER["benchmark_label"]},
        "generated_at": kyiv_now.strftime("%d.%m.%Y %H:%M") + " (Київ)",
        "start_date": START_DATE,
        "totals": totals,
        "weekly": weekly,
        "monthly": monthly,
        "reasons": reasons,
        "hourly": hourly,
        "stores": stores,
        "store_weekly": store_weekly,
        "complaints": complaints,
    }

    with open(os.path.join(BASE, "template.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/*__DATA__*/", "const DATA = " + json.dumps(payload, ensure_ascii=False) + ";")

    out = os.path.join(BASE, "docs", "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"OK: {out}")
    print(f"totals: {totals}")


if __name__ == "__main__":
    build()
