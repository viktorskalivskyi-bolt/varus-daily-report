#!/bin/bash
# Daily VARUS report: build via Databricks and push to GitHub (Pages redeploys).
# Scheduled by launchd: ~/Library/LaunchAgents/com.viktor.varus-report.plist
set -euo pipefail

REPO="/Users/viktorskalivskyi/Viktor_Cursor/Bolt/varus-daily-report"
LOG_PREFIX="[varus-report $(date '+%Y-%m-%d %H:%M:%S')]"
cd "$REPO"

echo "$LOG_PREFIX start"

# Corporate proxy intercepts TLS; python needs the proxy CA in its trust store.
BUNDLE="$REPO/.ca_bundle.pem"
if [ ! -s "$BUNDLE" ]; then
  /usr/bin/openssl s_client -connect bolt-data.cloud.databricks.com:443 -showcerts </dev/null 2>/dev/null \
    | /usr/bin/awk '/BEGIN CERT/,/END CERT/' > "$BUNDLE"
fi
export SSL_CERT_FILE="$BUNDLE"

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export DATABRICKS_HOST="https://bolt-data.cloud.databricks.com"
export DATABRICKS_WAREHOUSE_ID="6aaaeffb5cee657e"
DATABRICKS_TOKEN=$(databricks auth token --profile bolt-data | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
export DATABRICKS_TOKEN

python3 build_report.py

git pull --rebase --quiet || true
git add docs/index.html
if git diff --cached --quiet; then
  echo "$LOG_PREFIX no changes"
else
  git commit -q -m "Daily report update: $(date '+%Y-%m-%d %H:%M') Kyiv"
  git push -q
  echo "$LOG_PREFIX pushed"
fi
echo "$LOG_PREFIX done"
