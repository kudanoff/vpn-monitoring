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
  # Молча проглоченная неудача отправки — худшее, что может сделать сторож:
  # он выглядит работающим, а сообщений нет. Пишем в системный журнал.
  local out
  if out=$(curl -sS --max-time 20 -X POST \
      "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d "chat_id=${TELEGRAM_CHAT_ID}" -d "parse_mode=HTML" \
      --data-urlencode "text=$1" 2>&1); then
    case "$out" in
      *'"ok":true'*) return 0 ;;
      *) logger -t vpnmon-watchdog "Telegram отверг сообщение: $out" ;;
    esac
  else
    logger -t vpnmon-watchdog "не достучались до Telegram: $out"
  fi
  return 1
}

# Проверка связи с Telegram при старте: из России api.telegram.org
# заблокирован, и сторож там бесполезен — лучше знать об этом сразу.
selftest() {
  curl -sS --max-time 10 -o /dev/null \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>&1
}

if [[ "${1:-}" == "--selftest" ]]; then
  if selftest; then
    echo "Telegram доступен, сторож на этой машине работоспособен."
  else
    echo "Telegram недоступен с этой машины — сторожу здесь не место."
    echo "Перенесите его на сервер вне России."
    exit 1
  fi
  exit 0
fi

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
