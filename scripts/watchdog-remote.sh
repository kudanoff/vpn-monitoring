#!/usr/bin/env bash
# Сторож, который следит за самим мониторингом.
#
# Ставится НЕ на хаб, а на другую машину — иначе он умрёт вместе с тем,
# за чем должен следить. У нас это сервер панели.
#
# Проверяет глубокий health хаба: не "контейнер запущен", а "данные идут".
# Пишет в телеграм только при смене состояния и раз в час, пока не почините.
set -euo pipefail

CONF="${WATCHDOG_ENV:-/opt/vpnmon-watchdog/.env}"
STATE="${WATCHDOG_STATE:-/opt/vpnmon-watchdog/state}"
FAILS_BEFORE_ALERT=2          # два промаха подряд, чтобы не дёргаться на моргание сети
REPEAT_EVERY=3600             # напоминать раз в час, пока не почините

# shellcheck disable=SC1090
source "$CONF"

now=$(date +%s)
prev_state="ok"; prev_notified=0
[[ -f "$STATE" ]] && read -r prev_state prev_notified < "$STATE" || true
fails=0
[[ -f "$STATE.fails" ]] && fails=$(cat "$STATE.fails")

notify() {
  curl -sS --max-time 20 -X POST \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" -d "parse_mode=HTML" \
    --data-urlencode "text=$1" >/dev/null || true
}

if body=$(curl -fsS --max-time 15 "$HUB_HEALTH_URL" 2>&1); then
  echo 0 > "$STATE.fails"
  if [[ "$prev_state" == "down" ]]; then
    notify "✅ <b>Мониторинг снова жив</b>%0A${body}"
  fi
  echo "ok $now" > "$STATE"
  exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$STATE.fails"

[[ "$fails" -lt "$FAILS_BEFORE_ALERT" ]] && exit 0

if [[ "$prev_state" != "down" ]] || (( now - prev_notified >= REPEAT_EVERY )); then
  notify "🚨 <b>Мониторинг не отвечает</b>
Хаб не подтверждает сбор данных уже ${fails} проверки подряд.
Пока это так, отсутствие алертов ничего не значит.

<i>${body}</i>"
  echo "down $now" > "$STATE"
else
  echo "down $prev_notified" > "$STATE"
fi
