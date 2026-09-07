#!/usr/bin/env bash
# Собирает targets/nodes.yml из API панели: имена, адреса, состояние.
# Запускать на хабе:  make sync-nodes
#
# Файл перезаписывается целиком — правки руками в нём не живут.
# Что исключать, задаётся NODES_IGNORE в hub/.env.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/hub/.env"
TOKEN_FILE="$ROOT/hub/secrets/panel_api_token"
OUT="$ROOT/targets/nodes.yml"

command -v jq >/dev/null || { echo "нужен jq: apt-get install -y jq"; exit 1; }
[[ -s "$TOKEN_FILE" ]] || { echo "нет токена в $TOKEN_FILE"; exit 1; }

read_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true; }

API_URL="$(read_env PANEL_API_URL)"
[[ -n "$API_URL" ]] || { echo "задайте PANEL_API_URL в hub/.env (например https://panel.example.com)"; exit 1; }
IGNORE="$(read_env NODES_IGNORE)"; IGNORE="${IGNORE:-archive|ipcheker}"
XRAY_PORT="$(read_env DEFAULT_XRAY_PORT)"; XRAY_PORT="${XRAY_PORT:-443}"
COOKIE="$(read_env PANEL_COOKIE)"
OVERRIDES="$ROOT/targets/port-overrides.conf"
TOKEN="$(tr -d '\n\r' < "$TOKEN_FILE")"

# --http1.1 намеренно: панели за nginx нередко рвут HTTP/2-поток на API.
CURL_ARGS=(--http1.1 -sS -H "Authorization: Bearer ${TOKEN}")
# Кука защиты обратного прокси, если она включена в панели.
[[ -n "$COOKIE" ]] && CURL_ARGS+=(-H "Cookie: ${COOKIE}")

# Индивидуальные порты и исключения из проверки порта.
OV_JSON='{}'
if [[ -f "$OVERRIDES" ]]; then
  OV_JSON=$(grep -vE '^\s*(#|$)' "$OVERRIDES" \
    | sed -E 's/^[[:space:]]*//; s/[[:space:]]*$//' \
    | jq -R -s 'split("\n") | map(select(length>0))
        | map(capture("^(?<k>.+?)[[:space:]]*=[[:space:]]*(?<v>[^[:space:]]+)$"))
        | map({(.k): .v}) | add // {}' 2>/dev/null || echo '{}')
fi

echo "Запрашиваю ноды у ${API_URL} ..."
BODY_FILE=$(mktemp)
CODE=$(curl "${CURL_ARGS[@]}" -o "$BODY_FILE" -w '%{http_code}' "${API_URL%/}/api/nodes" || echo 000)
RAW=$(cat "$BODY_FILE"); rm -f "$BODY_FILE"

if [[ "$CODE" != "200" ]]; then
  echo "API ответил кодом ${CODE}."
  case "$CODE" in
    000) echo "Соединение не установилось: проверьте PANEL_API_URL и доступность панели с хаба." ;;
    401|403) echo "Не приняты доступы: проверьте токен в ${TOKEN_FILE} и PANEL_COOKIE в hub/.env." ;;
    404) echo "Эндпоинт не найден: в вашей версии панели путь к списку нод может отличаться." ;;
  esac
  echo "Ответ:"; echo "$RAW" | head -c 400
  exit 1
fi

# Разные версии панели заворачивают ответ по-разному: то массив, то объект
# с полем nodes, то и вовсе без обёртки. Разбираем все три случая.
NODES=$(echo "$RAW" | jq -c '
  (.response // .) as $r
  | if ($r | type) == "array" then $r
    elif ($r.nodes | type) == "array" then $r.nodes
    else [] end
')

COUNT=$(echo "$NODES" | jq 'length')
[[ "$COUNT" -gt 0 ]] || { echo "API вернул ноль нод. Сырой ответ:"; echo "$RAW" | head -c 500; exit 1; }

{
  echo "# Файл создан автоматически: make sync-nodes"
  echo "# Руками не править — правки затрёт следующая синхронизация."
  echo "# Отсюда берутся цели для проверок доступности из NL и РФ."
  echo "# Ноды с агентом перечислены отдельно, в targets/agents.yml."
  echo "# Источник: ${API_URL}/api/nodes, $(date '+%Y-%m-%d %H:%M')"
  echo
  echo "$NODES" | jq -r --arg ignore "$IGNORE" --arg port "$XRAY_PORT" --argjson ov "$OV_JSON" '
    .[]
    # Выключенные в панели ноды не мониторим: они выключены намеренно.
    | select((.isDisabled // false) == false)
    | select((.name // "") | test($ignore; "i") | not)
    | select((.address // "") != "")
    # Поле port у ноды — это её внутренний API (2222), закрытый для всех,
    # кроме панели. Порт, на котором xray принимает клиентов, панель в
    # списке нод не отдаёт: берём из DEFAULT_XRAY_PORT или из overrides.
    | . as $n
    | (($ov[$n.name] // $port)) as $xp
    | "- targets: [\"\($n.address):9100\"]\n  labels:\n    node: \"\($n.name)\"\n    public_ip: \"\($n.address)\"\n"
      + (if $xp == "skip" then "" else "    xray_port: \"\($xp)\"\n" end)
      + "    hoster: \"\($n.providerName // "")\"\n"
  '
} > "$OUT.tmp"

mv "$OUT.tmp" "$OUT"

WRITTEN=$(grep -c '^- targets:' "$OUT" || true)
echo "Записано нод: ${WRITTEN} из ${COUNT} (остальные выключены или в игноре)"
echo "Файл: $OUT"
echo
echo "vmagent перечитает его сам в течение 30 секунд."
