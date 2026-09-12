#!/usr/bin/env bash
# Отправляет искусственный алерт прямо в Alertmanager.
# Проверяет всю цепочку доставки: Alertmanager → бот нужного проекта → телеграм.
#   ./scripts/test-alert.sh most             — в чат проекта most
#   ./scripts/test-alert.sh main critical    — как выглядит критический
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_P=$(grep -E '^PROJECT_ID=' "$ROOT/hub/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"')

PROJECT="${1:-${DEFAULT_P:-main}}"
SEV="${2:-warning}"
NODE="${3:-ПРОВЕРКА}"
NOW=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)
END=$(date -u -d '+2 minutes' +%Y-%m-%dT%H:%M:%S.000Z 2>/dev/null \
      || date -u -v+2M +%Y-%m-%dT%H:%M:%S.000Z)

read -r -d '' PAYLOAD <<JSON || true
[{
  "labels": {
    "alertname": "ТестДоставки",
    "node": "${NODE}",
    "project": "${PROJECT}",
    "severity": "${SEV}",
    "action": "ничего не делать, это проверка"
  },
  "annotations": {
    "summary": "🧪 Проверка доставки алертов — проект ${PROJECT}",
    "description": "Если вы это видите, цепочка правила → Alertmanager → бот → телеграм работает."
  },
  "startsAt": "${NOW}",
  "endsAt": "${END}"
}]
JSON

docker run --rm --network vpnmon_mon -i curlimages/curl:latest \
  -sS -XPOST -H 'Content-Type: application/json' \
  --data-binary @- http://alertmanager:9093/api/v2/alerts <<< "$PAYLOAD"

echo "Алерт отправлен в чат проекта ${PROJECT}. Придёт в течение 10-40 секунд."
echo "Через 2 минуты придёт уведомление о том, что он погас."
