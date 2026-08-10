# -*- coding: utf-8 -*-
"""VARUS daily ops report generator.

Queries Databricks (SQL Statement Execution API) for VARUS UA order stats:
weekly/monthly failed orders, lost GMV and CS tickets, plus top problem stores.
Renders a self-contained HTML dashboard to docs/index.html.

Env vars required:
  DATABRICKS_HOST          e.g. https://bolt-data.cloud.databricks.com
  DATABRICKS_TOKEN         PAT or OAuth access token
  DATABRICKS_WAREHOUSE_ID  SQL warehouse id
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

HOST = os.environ["DATABRICKS_HOST"].rstrip("/")
TOKEN = os.environ["DATABRICKS_TOKEN"]
WAREHOUSE = os.environ["DATABRICKS_WAREHOUSE_ID"]

START_DATE = "2026-05-01"
BASE = os.path.dirname(os.path.abspath(__file__))

VARUS_FILTER = (
    "country_code = 'ua' AND lower(provider_name) LIKE '%varus%' "
    f"AND order_created_date_local >= '{START_DATE}'"
)


def sql(statement: str):
    """Run a SQL statement, wait for the result, return list of dict rows."""
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

    # Poll if still running after the initial wait window.
    while data["status"]["state"] in ("PENDING", "RUNNING"):
        time.sleep(3)
        sid = data["statement_id"]
        req = urllib.request.Request(
            f"{HOST}/api/2.0/sql/statements/{sid}",
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


def fetch_data():
    weekly = sql(f"""
        SELECT date_format(date_trunc('week', order_created_date_local), 'yyyy-MM-dd') AS period,
               COUNT(*) AS orders,
               SUM(CASE WHEN order_state = 'failed' THEN 1 ELSE 0 END) AS failed,
               ROUND(SUM(CASE WHEN order_state = 'failed' THEN COALESCE(order_gmv_local, 0) ELSE 0 END), 0) AS lost_gmv
        FROM main.ng_delivery.dim_order_delivery
        WHERE {VARUS_FILTER}
        GROUP BY 1 ORDER BY 1
    """)

    monthly = sql(f"""
        SELECT date_format(date_trunc('month', order_created_date_local), 'yyyy-MM') AS period,
               COUNT(*) AS orders,
               SUM(CASE WHEN order_state = 'failed' THEN 1 ELSE 0 END) AS failed,
               ROUND(SUM(CASE WHEN order_state = 'failed' THEN COALESCE(order_gmv_local, 0) ELSE 0 END), 0) AS lost_gmv
        FROM main.ng_delivery.dim_order_delivery
        WHERE {VARUS_FILTER}
        GROUP BY 1 ORDER BY 1
    """)

    tickets_weekly = sql(f"""
        WITH varus AS (
            SELECT order_id FROM main.ng_delivery.dim_order_delivery WHERE {VARUS_FILTER}
        )
        SELECT date_format(date_trunc('week', sc.created), 'yyyy-MM-dd') AS period,
               COUNT(*) AS tickets
        FROM main.ng_customer_support.customer_support_support_case sc
        JOIN varus v ON CAST(sc.order_id AS STRING) = CAST(v.order_id AS STRING)
        WHERE sc.created >= '{START_DATE}'
        GROUP BY 1 ORDER BY 1
    """)

    tickets_monthly = sql(f"""
        WITH varus AS (
            SELECT order_id FROM main.ng_delivery.dim_order_delivery WHERE {VARUS_FILTER}
        )
        SELECT date_format(date_trunc('month', sc.created), 'yyyy-MM') AS period,
               COUNT(*) AS tickets
        FROM main.ng_customer_support.customer_support_support_case sc
        JOIN varus v ON CAST(sc.order_id AS STRING) = CAST(v.order_id AS STRING)
        WHERE sc.created >= '{START_DATE}'
        GROUP BY 1 ORDER BY 1
    """)

    top_stores = sql(f"""
        SELECT provider_name,
               COUNT(*) AS orders,
               SUM(CASE WHEN order_state = 'failed' THEN 1 ELSE 0 END) AS failed,
               ROUND(100.0 * SUM(CASE WHEN order_state = 'failed' THEN 1 ELSE 0 END) / COUNT(*), 2) AS ndr_pct,
               ROUND(SUM(CASE WHEN order_state = 'failed' THEN COALESCE(order_gmv_local, 0) ELSE 0 END), 0) AS lost_gmv
        FROM main.ng_delivery.dim_order_delivery
        WHERE {VARUS_FILTER}
        GROUP BY 1
        HAVING SUM(CASE WHEN order_state = 'failed' THEN 1 ELSE 0 END) > 0
        ORDER BY lost_gmv DESC
        LIMIT 12
    """)

    return weekly, monthly, tickets_weekly, tickets_monthly, top_stores


def build():
    weekly, monthly, tw, tm, top_stores = fetch_data()

    tw_map = {r["period"]: num(r["tickets"]) for r in tw}
    tm_map = {r["period"]: num(r["tickets"]) for r in tm}

    weekly_data = [{
        "period": r["period"],
        "orders": num(r["orders"]),
        "failed": num(r["failed"]),
        "lost_gmv": num(r["lost_gmv"]),
        "tickets": tw_map.get(r["period"], 0),
    } for r in weekly]

    monthly_data = [{
        "period": r["period"],
        "orders": num(r["orders"]),
        "failed": num(r["failed"]),
        "lost_gmv": num(r["lost_gmv"]),
        "tickets": tm_map.get(r["period"], 0),
    } for r in monthly]

    stores_data = [{
        "name": r["provider_name"].replace("VARUS, ", ""),
        "orders": num(r["orders"]),
        "failed": num(r["failed"]),
        "ndr_pct": num(r["ndr_pct"]),
        "lost_gmv": num(r["lost_gmv"]),
    } for r in top_stores]

    with open(os.path.join(BASE, "data", "complaints.json"), encoding="utf-8") as f:
        complaints = json.load(f)

    totals = {
        "orders": sum(r["orders"] for r in monthly_data),
        "failed": sum(r["failed"] for r in monthly_data),
        "lost_gmv": sum(r["lost_gmv"] for r in monthly_data),
        "tickets": sum(r["tickets"] for r in monthly_data),
    }
    totals["ndr_pct"] = round(100.0 * totals["failed"] / totals["orders"], 2) if totals["orders"] else 0

    kyiv_now = datetime.now(timezone.utc) + timedelta(hours=3)
    payload = {
        "generated_at": kyiv_now.strftime("%d.%m.%Y %H:%M") + " (Київ)",
        "start_date": START_DATE,
        "totals": totals,
        "weekly": weekly_data,
        "monthly": monthly_data,
        "top_stores": stores_data,
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
