#!/usr/bin/env bash
# Отправляет искусственный алерт прямо в Alertmanager.
# Проверяет всю цепочку доставки: Alertmanager → бот → телеграм.
#   ./scripts/test-alert.sh              — обычная проверка
#   ./scripts/test-alert.sh critical     — как выглядит критический
set -euo pipefail

SEV="${1:-warning}"
NODE="${2:-ПРОВЕРКА}"
NOW=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)
END=$(date -u -d '+2 minutes' +%Y-%m-%dT%H:%M:%S.000Z 2>/dev/null \
      || date -u -v+2M +%Y-%m-%dT%H:%M:%S.000Z)

read -r -d '' PAYLOAD <<JSON || true
[{
  "labels": {
    "alertname": "ТестДоставки",
    "node": "${NODE}",
    "severity": "${SEV}",
    "action": "ничего не делать, это проверка"
  },
  "annotations": {
    "summary": "🧪 Проверка доставки алертов",
    "description": "Если вы это видите, цепочка правила → Alertmanager → бот → телеграм работает."
  },
  "startsAt": "${NOW}",
  "endsAt": "${END}"
}]
JSON

docker run --rm --network vpnmon_mon -i curlimages/curl:latest \
  -sS -XPOST -H 'Content-Type: application/json' \
  --data-binary @- http://alertmanager:9093/api/v2/alerts <<< "$PAYLOAD"

echo "Алерт отправлен. Сообщение придёт в течение 10-40 секунд."
echo "Через 2 минуты придёт уведомление о том, что он погас."
