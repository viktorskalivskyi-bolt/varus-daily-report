# Operational Report

Автооновлюваний операційний звіт по партнерах Bolt Food UA (зараз — VARUS, вибір партнера
в шапці звіту): бізнес-метрики (GMV / замовлення / AOV), покриття магазинів по містах,
зафейлені замовлення і втрачений GMV, CS-тікети — по місяцях і тижнях, плюс кураторський
список ескалацій зі Slack `#ua-delivery-daily`.

## Як це працює

- `build_report.py` для кожного партнера зі списку `PARTNERS` тягне свіжі агрегати
  з Databricks (SQL Statement Execution API):
  - `main.ng_delivery.dim_order_delivery` — замовлення, фейли, GMV, покриття по містах;
  - `main.ng_customer_support.customer_support_support_case` — CS-тікети, привʼязані до цих замовлень.
- Щоб додати нового партнера — додати запис у `PARTNERS` (slug, назва, LIKE-фільтр,
  бенчмарк-вертикаль); він зʼявиться в дропдауні автоматично.
- Дані вшиваються в `template.html` (Chart.js) → `docs/index.html`, який роздається через GitHub Pages:
  **https://viktorskalivskyi-bolt.github.io/varus-daily-report/**
- Розклад: launchd-джоба на робочому Mac (`~/Library/LaunchAgents/com.viktor.varus-report.plist`)
  щодня о **06:00 за Києвом** запускає `run_local.sh` → збірка + push (якщо Mac спав, джоба
  відпрацює при першому пробудженні).
- `data/complaints.json` — статичний кураторський список 79 ескалацій (02.05–10.08.2026),
  зібраний зі Slack і збагачений даними Databricks.

## Локальний запуск

```bash
export DATABRICKS_HOST=https://bolt-data.cloud.databricks.com
export DATABRICKS_TOKEN=$(databricks auth token --profile bolt-data | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
export DATABRICKS_WAREHOUSE_ID=6aaaeffb5cee657e
python3 build_report.py
open docs/index.html
```
