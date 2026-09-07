#!/usr/bin/env bash
# Проверяет полную цепочку: правило → vmalert → Alertmanager → бот → телеграм.
# В отличие от test-alert.sh, который начинается с Alertmanager, здесь
# проверяется и то, что vmalert вообще считает правила и умеет их отправлять.
#
#   ./scripts/test-rule.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RULE="$ROOT/hub/vmalert/rules/_test.yml"

cleanup() {
  rm -f "$RULE"
  (cd "$ROOT/hub" && docker compose restart vmalert >/dev/null)
  echo "Временное правило удалено."
}
trap cleanup EXIT

cat > "$RULE" <<'YAML'
# Временный файл, создаётся ./scripts/test-rule.sh и удаляется им же.
groups:
- name: _test
  interval: 15s
  rules:
  - alert: ПроверкаПравил
    expr: vector(1)
    for: 0s
    labels:
      severity: warning
      node: ПРОВЕРКА
      action: "ничего не делать, это проверка"
    annotations:
      summary: "🧪 Проверка цепочки правил"
      description: "Правило посчиталось, дошло до Alertmanager и до телеграма."
YAML

cd "$ROOT/hub" && docker compose restart vmalert >/dev/null
echo "Правило добавлено, жду срабатывания. Сообщение придёт в течение минуты."
sleep 100
