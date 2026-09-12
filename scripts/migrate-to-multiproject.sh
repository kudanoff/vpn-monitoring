#!/usr/bin/env bash
# Разовая миграция установки с одного проекта на несколько.
#
# Переименовывает секреты, списки нод и переменные .env так, чтобы у каждого
# проекта был свой набор. Запускать один раз на хабе:
#   ./scripts/migrate-to-multiproject.sh main
set -euo pipefail

P="${1:-main}"
PU=$(echo "$P" | tr '[:lower:]-' '[:upper:]_')
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV="$ROOT/hub/.env"

[[ -f "$ENV" ]] || { echo "не найден $ENV"; exit 1; }
cp "$ENV" "$ENV.before-multiproject"
echo "Резервная копия: $ENV.before-multiproject"

move() { [[ -e "$1" && ! -e "$2" ]] && { mv "$1" "$2"; echo "  $1 → $2"; } || true; }

echo "Секреты:"
move "$ROOT/hub/secrets/panel_metrics_user"     "$ROOT/hub/secrets/panel_metrics_user_${P}"
move "$ROOT/hub/secrets/panel_metrics_password" "$ROOT/hub/secrets/panel_metrics_password_${P}"
move "$ROOT/hub/secrets/panel_api_token"        "$ROOT/hub/secrets/panel_api_token_${P}"

echo "Списки нод:"
move "$ROOT/targets/nodes.yml"            "$ROOT/targets/nodes-${P}.yml"
move "$ROOT/targets/agents.yml"           "$ROOT/targets/agents-${P}.yml"
move "$ROOT/targets/port-overrides.conf"  "$ROOT/targets/port-overrides-${P}.conf"

echo "Переменные .env:"
rename_var() {
  if grep -qE "^$1=" "$ENV" && ! grep -qE "^$2=" "$ENV"; then
    sed -i.bak "s/^$1=/$2=/" "$ENV" && rm -f "$ENV.bak"
    echo "  $1 → $2"
  fi
}
rename_var PANEL_ADDR        "PANEL_ADDR_${PU}"
rename_var RU_PROBER_ADDR    "PROBER_ADDR_${PU}"
rename_var PANEL_API_URL     "PANEL_API_URL_${PU}"
rename_var PANEL_COOKIE      "PANEL_COOKIE_${PU}"
rename_var DEFAULT_XRAY_PORT "DEFAULT_XRAY_PORT_${PU}"

grep -qE '^PROJECTS=' "$ENV" || { printf 'PROJECTS="%s"\n' "$P" >> "$ENV"; echo "  добавлен PROJECTS"; }
grep -qE '^PROJECT_ID=' "$ENV" || { printf 'PROJECT_ID=%s\n' "$P" >> "$ENV"; echo "  добавлен PROJECT_ID"; }

echo
echo "Готово. Дальше:"
echo "  1. Допишите в PROJECTS новые проекты через пробел"
echo "  2. Добавьте их переменные и секреты"
echo "  3. make up"
