# VARUS Daily Ops Report

Автооновлюваний звіт по мережі VARUS (Bolt Food UA): зафейлені замовлення, втрачений GMV,
CS-тікети — по місяцях і тижнях, плюс кураторський список ескалацій зі Slack `#ua-delivery-daily`.

## Як це працює

- `build_report.py` тягне свіжі агрегати з Databricks (SQL Statement Execution API):
  - `main.ng_delivery.dim_order_delivery` — замовлення, фейли, GMV по всіх сторах VARUS UA;
  - `main.ng_customer_support.customer_support_support_case` — CS-тікети, привʼязані до цих замовлень.
- Дані вшиваються в `template.html` (Chart.js) → `docs/index.html`.
- GitHub Actions (`.github/workflows/daily-report.yml`) запускається щодня о **06:00 за Києвом**
  (cron на 03:00 і 04:00 UTC + guard по київській годині) і комітить оновлений звіт.
- `data/complaints.json` — статичний кураторський список 79 ескалацій (02.05–10.08.2026),
  зібраний зі Slack і збагачений даними Databricks.

## Секрети (Actions)

| Secret | Значення |
|---|---|
| `DATABRICKS_HOST` | `https://bolt-data.cloud.databricks.com` |
| `DATABRICKS_TOKEN` | PAT (створений 10.08.2026, діє 90 днів — оновити до ~08.11.2026) |
| `DATABRICKS_WAREHOUSE_ID` | SQL warehouse для запитів |

## Локальний запуск

```bash
export DATABRICKS_HOST=https://bolt-data.cloud.databricks.com
export DATABRICKS_TOKEN=$(databricks auth token --profile bolt-data | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
export DATABRICKS_WAREHOUSE_ID=6aaaeffb5cee657e
python3 build_report.py
open docs/index.html
```
