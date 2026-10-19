#!/usr/bin/env bash
# Собирает targets/nodes-<проект>.yml из API панели этого проекта.
#   make sync-nodes              — все проекты из PROJECTS
#   ./scripts/sync-nodes.sh most — только один
#
# Файл перезаписывается целиком: правки руками в нём не живут.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/hub/.env"

read_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true; }

# Без аргумента обходим все проекты, перезапуская себя же для каждого.
if [[ $# -eq 0 ]]; then
  PROJECTS="$(read_env PROJECTS)"
  [[ -n "$PROJECTS" ]] || { echo 'задайте PROJECTS в hub/.env, например: PROJECTS="main most"'; exit 1; }
  rc=0
  for p in $PROJECTS; do
    echo "─── $p ───"
    "$0" "$p" || rc=1
  done
  exit $rc
fi

PROJECT="$1"
PU=$(echo "$PROJECT" | tr '[:lower:]-' '[:upper:]_')
TOKEN_FILE="$ROOT/hub/secrets/panel_api_token_${PROJECT}"
OUT="$ROOT/targets/nodes-${PROJECT}.yml"

command -v jq >/dev/null || { echo "нужен jq: apt-get install -y jq"; exit 1; }
[[ -s "$TOKEN_FILE" ]] || { echo "нет токена в $TOKEN_FILE"; exit 1; }

# Переменные проекта: PANEL_API_URL_MOST и так далее.
proj_env() { read_env "${1}_${PU}"; }

API_URL="$(proj_env PANEL_API_URL)"
[[ -n "$API_URL" ]] || { echo "задайте PANEL_API_URL_${PU} в hub/.env (например https://panel.example.com)"; exit 1; }
IGNORE="$(proj_env NODES_IGNORE)"; IGNORE="${IGNORE:-$(read_env NODES_IGNORE)}"; IGNORE="${IGNORE:-ipcheker}"
XRAY_PORT="$(proj_env DEFAULT_XRAY_PORT)"; XRAY_PORT="${XRAY_PORT:-443}"
COOKIE="$(proj_env PANEL_COOKIE)"
OVERRIDES="$ROOT/targets/port-overrides-${PROJECT}.conf"
IP_OVERRIDES="$ROOT/targets/public-ip-overrides-${PROJECT}.conf"
# Первый запуск: создаём файлы исключений из образцов, чтобы было что править.
[[ -f "$OVERRIDES" ]] || cp "$ROOT/targets/port-overrides.conf.example" "$OVERRIDES" 2>/dev/null || true
[[ -f "$IP_OVERRIDES" ]] || cp "$ROOT/targets/public-ip-overrides.conf.example" "$IP_OVERRIDES" 2>/dev/null || true
# Диапазоны оверлейных сетей (NetBird, Tailscale): адрес ноды из них виден
# только участникам этой сети, а хаб и клиенты в неё не входят.
OVERLAY="$(proj_env OVERLAY_RANGES)"; OVERLAY="${OVERLAY:-$(read_env OVERLAY_RANGES)}"; OVERLAY="${OVERLAY:-100.64.0.0/10}"
TOKEN="$(tr -d '\n\r' < "$TOKEN_FILE")"

# --http1.1 намеренно: панели за nginx нередко рвут HTTP/2-поток на API.
CURL_ARGS=(--http1.1 -sS -H "Authorization: Bearer ${TOKEN}")
# Кука защиты обратного прокси, если она включена в панели.
[[ -n "$COOKIE" ]] && CURL_ARGS+=(-H "Cookie: ${COOKIE}")

# Ходить в API по приватному адресу надёжнее, чем через домен: не зависит от
# Cloudflare и не светит токен наружу. Но Remnawave 3.x проверяет, что перед
# ней прокси с HTTPS, и голый http обрывает молча — curl показывает это как
# "Empty reply from server". Домен для Host задаётся PANEL_API_HOST_<ПРОЕКТ>.
API_HOST="$(proj_env PANEL_API_HOST)"
[[ -n "$API_HOST" ]] && CURL_ARGS+=(-H "Host: ${API_HOST}")
if [[ "$API_URL" == http://* ]]; then
  HUB_IP="$(read_env HUB_PRIVATE_IP)"
  CURL_ARGS+=(-H "X-Forwarded-Proto: https" -H "X-Forwarded-For: ${HUB_IP:-10.77.0.1}")
fi

# Файл вида "имя ноды = значение" → JSON-объект {имя: значение}.
parse_conf() {
  [[ -f "$1" ]] || { echo '{}'; return; }
  # grep без совпадений возвращает ошибку и с pipefail роняет всю цепочку,
  # а файл из одних комментариев — совершенно нормальное состояние.
  local parsed
  parsed=$({ grep -vE '^[[:space:]]*(#|$)' "$1" || true; } \
    | sed -E 's/^[[:space:]]*//; s/[[:space:]]*$//' \
    | jq -R -s 'split("\n") | map(select(length>0))
        | map(capture("^(?<k>.+?)[[:space:]]*=[[:space:]]*(?<v>[^[:space:]]+)$"))
        | map({(.k): .v}) | add // {}' 2>/dev/null || true)
  if [[ -n "$parsed" ]]; then echo "$parsed"; else echo '{}'; fi
}

# Индивидуальные порты и исключения из проверки порта.
OV_JSON=$(parse_conf "$OVERRIDES")
[[ "$OV_JSON" != '{}' ]] && echo "Индивидуальные порты: $OV_JSON"
# Реальные IP нод, которые панель знает по адресу в оверлейной сети.
IP_JSON=$(parse_conf "$IP_OVERRIDES")
[[ "$IP_JSON" != '{}' ]] && echo "Реальные IP: $IP_JSON"
OVERLAY_JSON=$(jq -cn --arg r "$OVERLAY" '$r | split("[ ,]+"; null) | map(select(length>0))')
for r in $(jq -r '.[]' <<<"$OVERLAY_JSON"); do
  [[ "$r" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] \
    || { echo "OVERLAY_RANGES: «$r» — нужен вид 100.64.0.0/10"; exit 1; }
done
echo "Оверлейные сети: $OVERLAY_JSON"

echo "Запрашиваю ноды у ${API_URL} ..."
BODY_FILE=$(mktemp)
CODE=$(curl "${CURL_ARGS[@]}" -o "$BODY_FILE" -w '%{http_code}' "${API_URL%/}/api/nodes" || echo 000)
RAW=$(cat "$BODY_FILE"); rm -f "$BODY_FILE"

if [[ "$CODE" != "200" ]]; then
  echo "API ответил кодом ${CODE}."
  case "$CODE" in
    000)
      echo "Ответа нет. Либо панель недоступна с хаба, либо она закрыла соединение:"
      echo "Remnawave за обратным прокси требует Host и признак HTTPS —"
      echo "задайте PANEL_API_HOST_${PU}=<домен панели> в hub/.env." ;;
    401|403)
      if grep -qi 'just a moment\|cf-browser-verification\|cloudflare' <<<"$RAW"; then
        echo "Ответил не API, а Cloudflare: домен закрыт челленджем."
        echo "Либо правило WAF → Skip для /api/*, либо ходите по приватному адресу:"
        echo "  PANEL_API_URL_${PU}=http://<адрес панели в WireGuard>:3000"
        echo "  PANEL_API_HOST_${PU}=<домен панели>"
      else
        echo "Не приняты доступы: проверьте токен в ${TOKEN_FILE} и PANEL_COOKIE в hub/.env."
      fi ;;
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

# Реальные IP нод в NetBird: его API знает про каждый пир и адрес в сети
# NetBird, и белый IP, с которого пир к ней подключён. Нужен токен —
# hub/secrets/netbird_api_token (общий) или netbird_api_token_<проект>.
NB_JSON='{}'
NB_OK=""
NB_TOKEN_FILE="$ROOT/hub/secrets/netbird_api_token_${PROJECT}"
[[ -s "$NB_TOKEN_FILE" ]] || NB_TOKEN_FILE="$ROOT/hub/secrets/netbird_api_token"
if [[ -s "$NB_TOKEN_FILE" ]]; then
  NB_URL="$(proj_env NETBIRD_API_URL)"; NB_URL="${NB_URL:-$(read_env NETBIRD_API_URL)}"; NB_URL="${NB_URL:-https://api.netbird.io}"
  NB_TOKEN="$(tr -d '\n\r' < "$NB_TOKEN_FILE")"
  NB_BODY=$(mktemp)
  NB_CODE=$(curl -sS -H "Authorization: Token ${NB_TOKEN}" -H "Accept: application/json" \
    -o "$NB_BODY" -w '%{http_code}' "${NB_URL%/}/api/peers" || echo 000)
  if [[ "$NB_CODE" == "200" ]]; then
    NB_OK=1
    # Только IPv4: пробы ходят по ip4, а пир, подключённый по IPv6,
    # лучше показать в списке без IP, чем молча не допинговаться.
    NB_JSON=$(jq -c '[ .[] | select((.ip // "") != "")
        | select((.connection_ip // "") | test("^[0-9]{1,3}(\\.[0-9]{1,3}){3}$"))
        | {(.ip): .connection_ip} ] | add // {}' "$NB_BODY" 2>/dev/null || echo '{}')
    echo "Пиров NetBird с белым IP: $(jq 'length' <<<"$NB_JSON")"
  else
    echo "⚠️  API NetBird ответил кодом ${NB_CODE} — реальные IP берутся только из ${IP_OVERRIDES#$ROOT/}."
    [[ "$NB_CODE" == "401" ]] && echo "    Проверьте токен в ${NB_TOKEN_FILE#$ROOT/}."
  fi
  rm -f "$NB_BODY"
fi

# Для каждой ноды решаем, по какому адресу её проверять снаружи:
# строка в public-ip-overrides важнее всего, потом NetBird, потом панель.
TARGETS=$(echo "$NODES" | jq -c --arg ignore "$IGNORE" --argjson ips "$IP_JSON" --argjson nb "$NB_JSON" --argjson overlay "$OVERLAY_JSON" '
  def ip2n: split(".") | map(tonumber) | .[0]*16777216 + .[1]*65536 + .[2]*256 + .[3];
  def in_cidr($c): ($c | split("/")) as $p
    | pow(2; 32 - ($p[1] | tonumber)) as $size
    | ((ip2n / $size) | floor) == ((($p[0] | ip2n) / $size) | floor);
  [ .[]
    # Выключенные в панели ноды не мониторим: они выключены намеренно.
    | select((.isDisabled // false) == false)
    | select((.name // "") | test($ignore; "i") | not)
    | select((.address // "") != "")
    | . as $n
    # Адрес из оверлейной сети (NetBird и т.п.) пингуется только изнутри неё:
    # панель его видит, а хаб и клиенты — нет. Такой ноде нужен реальный IP.
    | ($n.address | test("^[0-9]{1,3}(\\.[0-9]{1,3}){3}$")
        and (. as $a | any($overlay[]; . as $c | $a | in_cidr($c)))) as $is_overlay
    | (if $ips[$n.name] then {ip: $ips[$n.name], via: "файл"}
       elif $is_overlay and $nb[$n.address] then {ip: $nb[$n.address], via: "NetBird"}
       elif $is_overlay then {ip: null, via: null}
       else {ip: $n.address, via: null} end) as $r
    | {name: $n.name, address: $n.address, ip: $r.ip, via: $r.via, hoster: ($n.providerName // "")}
  ]
')

{
  echo "# Файл создан автоматически: make sync-nodes"
  echo "# Руками не править — правки затрёт следующая синхронизация."
  echo "# Отсюда берутся цели для проверок доступности из NL и РФ."
  echo "# Ноды с агентом перечислены отдельно, в targets/agents.yml."
  echo "# Проект: ${PROJECT}. Источник: ${API_URL}/api/nodes, $(date '+%Y-%m-%d %H:%M')"
  echo
  echo "$TARGETS" | jq -r --arg port "$XRAY_PORT" --argjson ov "$OV_JSON" '
    .[]
    | select(.ip != null)
    | . as $n
    # Поле port у ноды — это её внутренний API (2222), закрытый для всех,
    # кроме панели. Порт, на котором xray принимает клиентов, панель в
    # списке нод не отдаёт: берём из DEFAULT_XRAY_PORT или из overrides.
    | ($ov[$n.name] // $port) as $xp
    # Значение может быть портом (8443), парой хост:порт для нод за CDN
    # или словом skip, чтобы не проверять порт вовсе. Порт проверяем
    # на реальном IP: клиенты подключаются туда, а не в оверлей.
    | (if $xp == "skip" then ""
       elif ($xp | test(":")) then $xp
       else "\($n.ip):\($xp)" end) as $target
    | "- targets: [\"\($n.ip):9100\"]\n  labels:\n    node: \"\($n.name)\"\n    public_ip: \"\($n.ip)\"\n"
      + (if $target == "" then "" else "    tcp_target: \"\($target)\"\n" end)
      + "    hoster: \"\($n.hoster)\"\n"
  '
} > "$OUT.tmp"

mv "$OUT.tmp" "$OUT"

WRITTEN=$(grep -c '^- targets:' "$OUT" || true)
echo "Записано нод: ${WRITTEN} из ${COUNT} (остальные выключены, в игноре или без реального IP)"
echo "Файл: $OUT"

# Ноды в оверлее без реального IP не проверяются вовсе — лучше молчать,
# чем каждую минуту показывать живую ноду упавшей. Но сказать об этом надо.
RESOLVED=$(echo "$TARGETS" | jq -r '.[] | select(.via != null) | "  \(.name): \(.address) → \(.ip)  (\(.via))"')
if [[ -n "$RESOLVED" ]]; then
  echo
  echo "Проверяются по реальному IP, а не по адресу из панели:"
  echo "$RESOLVED"
fi

UNRESOLVED=$(echo "$TARGETS" | jq -r '.[] | select(.ip == null) | "  \(.name) = <реальный IP>    # в панели \(.address)"')
if [[ -n "$UNRESOLVED" ]]; then
  echo
  echo "⚠️  Эти ноды панель знает по адресу из оверлейной сети ($OVERLAY),"
  echo "    снаружи он не пингуется, поэтому проверки по ним выключены."
  if [[ -n "$NB_OK" ]]; then
    echo "    В NetBird для них белого IP нет (пир не подключён или подключён по IPv6)."
    echo "    Впишите их руками в ${IP_OVERRIDES#$ROOT/}:"
  elif [[ -s "$NB_TOKEN_FILE" ]]; then
    echo "    Спросить NetBird не вышло (см. выше). Почините токен или впишите"
    echo "    их руками в ${IP_OVERRIDES#$ROOT/}:"
  else
    echo "    Проще всего положить токен NetBird в hub/secrets/netbird_api_token —"
    echo "    тогда IP подтянутся сами. Или впишите их руками в ${IP_OVERRIDES#$ROOT/}:"
  fi
  echo "$UNRESOLVED"
fi
echo
echo "vmagent перечитает его сам в течение 30 секунд."
