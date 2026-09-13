#!/usr/bin/env bash
# Ускоряет все алерты до минуты — чтобы проверять сценарии, не ожидая
# по десять минут. Правила лежат в гите, поэтому выключение просто
# возвращает их к исходному виду.
#
#   ./scripts/test-mode.sh on    — все пороги ожидания в 1 минуту
#   ./scripts/test-mode.sh off   — вернуть боевые значения
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RULES="$ROOT/hub/vmalert/rules"

case "${1:-}" in
  on)
    sed -i -E 's/^([[:space:]]*)for: [0-9]+[smh]$/\1for: 1m/' "$RULES"/*.yml
    echo "Режим проверки включён: все алерты срабатывают через минуту."
    echo "ВАЖНО: не забудьте ./scripts/test-mode.sh off — иначе будете"
    echo "получать тревогу от любого моргания сети."
    ;;
  off)
    git -C "$ROOT" checkout -- hub/vmalert/rules
    echo "Боевые пороги возвращены."
    ;;
  *)
    echo "Использование: $0 on|off"
    exit 1
    ;;
esac

cd "$ROOT/hub" && docker compose restart vmalert >/dev/null
echo "vmalert перечитал правила."
grep -h '    for:' "$RULES"/*.yml | sort | uniq -c | sed 's/^/  /'
