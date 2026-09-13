#!/usr/bin/env bash
# Сквозная проверка мониторинга: от сбора метрик до доставки в телеграм.
# Запускать на хабе:  make check
#
# Отвечает на главный вопрос: молчание в чате — это "всё хорошо"
# или "мониторинг сломан и молчит".
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/hub"

NET=$(docker network ls --format '{{.Name}}' | grep -E '_mon$' | head -1)
PROJECTS=$(grep -E '^PROJECTS=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"')
PROJECTS="${PROJECTS:-main}"

ok=0; bad=0
say_ok()   { echo "  ✅ $1"; ok=$((ok+1)); }
say_bad()  { echo "  ❌ $1"; bad=$((bad+1)); }
say_warn() { echo "  ⚠️  $1"; }

q() {  # PromQL-запрос к VictoriaMetrics, возвращает первое значение
  docker run --rm --network "$NET" curlimages/curl:latest -s -G \
    'http://victoriametrics:8428/api/v1/query' --data-urlencode "query=$1" 2>/dev/null \
    | grep -o '"value":\[[^]]*\]' | head -1 | grep -o '"[0-9.e+-]*"$' | tr -d '"'
}

api() { docker run --rm --network "$NET" curlimages/curl:latest -s "$1" 2>/dev/null; }

echo "═══ 1. Контейнеры ═══"
for c in victoriametrics vmagent vmalert alertmanager blackbox bot; do
  st=$(docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | grep "^$c " | awk '{print $2}')
  [[ "$st" == "running" ]] && say_ok "$c" || say_bad "$c: ${st:-не запущен}"
done
for p in $PROJECTS; do
  [[ "$p" == "$(grep -E '^PROJECT_ID=' .env | cut -d= -f2)" ]] && continue
  st=$(docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | grep "^bot-$p " | awk '{print $2}')
  [[ "$st" == "running" ]] && say_ok "bot-$p" || say_bad "bot-$p: ${st:-не запущен}"
done

echo
echo "═══ 2. Сбор метрик ═══"
down=$(api 'http://vmagent:8429/api/v1/targets' | grep -o '"health":"[^"]*"' | grep -c 'down')
total=$(api 'http://vmagent:8429/api/v1/targets' | grep -c '"scrapeUrl"')
[[ "${down:-0}" -eq 0 ]] && say_ok "все цели скрейпятся ($total)" || say_bad "недоступных целей: $down из $total"

for p in $PROJECTS; do
  n=$(q "count(remnawave_node_status{project=\"$p\"})")
  [[ -n "$n" && "$n" != "0" ]] && say_ok "панель $p отдаёт метрики ($n нод)" || say_bad "панель $p: метрик нет"
  pr=$(q "count(probe_success{project=\"$p\"})")
  [[ -n "$pr" && "$pr" != "0" ]] && say_ok "проверки доступности $p ($pr)" || say_warn "проверки $p не идут — пуст список нод?"
done

echo
echo "═══ 3. Правила ═══"
rules=$(api 'http://vmalert:8880/api/v1/rules' | grep -o '"name":"[^"]*"' | wc -l | tr -d ' ')
[[ "${rules:-0}" -gt 20 ]] && say_ok "правил загружено: $rules" || say_bad "правил загружено всего $rules"

for p in $PROJECTS; do
  c=$(q "count(node:panel_cpu{project=\"$p\"})")
  [[ -n "$c" && "$c" != "0" ]] && say_ok "производные метрики $p считаются ($c)" || say_bad "$p: node:panel_cpu пуст — правила не отработали"
done

echo
echo "═══ 4. Срабатывали ли алерты ═══"
fired=$(q 'count(count by(alertname) (max_over_time(ALERTS{alertname!="Watchdog"}[7d])))')
if [[ -n "$fired" && "$fired" != "0" ]]; then
  say_ok "за неделю срабатывало разных алертов: $fired"
  docker run --rm --network "$NET" curlimages/curl:latest -s -G \
    'http://victoriametrics:8428/api/v1/query' \
    --data-urlencode 'query=count by(alertname, project) (max_over_time(ALERTS{alertname!="Watchdog"}[7d]))' 2>/dev/null \
    | grep -o '"alertname":"[^"]*","project":"[^"]*"' | sed 's/"alertname":"/     • /; s/","project":"/ — проект /; s/"$//'
else
  say_warn "за неделю не срабатывало ни одного алерта"
  echo "     Это нормально, если проблем не было. Проверьте доставку: make test-alert"
fi

wd=$(q 'max_over_time(ALERTS{alertname="Watchdog"}[10m])')
[[ "$wd" == "1" ]] && say_ok "сторож считается — значит правила реально вычисляются" \
                   || say_bad "сторож молчит: vmalert не вычисляет правила"

echo
echo "═══ 5. Доставка ═══"
amcfg=$(api 'http://alertmanager:9093/api/v2/status')
for p in $PROJECTS; do
  pid=$(grep -E '^PROJECT_ID=' .env | cut -d= -f2)
  rcv="bot"; [[ "$p" != "$pid" ]] && rcv="bot-$p"
  echo "$amcfg" | grep -q "$rcv" && say_ok "маршрут для $p ($rcv) загружен" || say_bad "маршрут для $p ($rcv) не загружен"
done

for p in $PROJECTS; do
  pid=$(grep -E '^PROJECT_ID=' .env | cut -d= -f2)
  svc="bot"; [[ "$p" != "$pid" ]] && svc="bot-$p"
  h=$(api "http://${svc}:8080/healthz")
  [[ "$h" == ok* ]] && say_ok "$svc отвечает: $h" || say_bad "$svc: ${h:-не отвечает}"
  err=$(docker compose logs --since=24h "$svc" 2>/dev/null | grep -c 'не отправился алерт\|не удалось написать')
  [[ "${err:-0}" -eq 0 ]] && say_ok "$svc: ошибок отправки за сутки нет" \
                          || say_bad "$svc: неудачных отправок за сутки: $err"
done

echo
echo "═══ Итог ═══"
echo "  Проверок пройдено: $ok, провалено: $bad"
if [[ "$bad" -eq 0 ]]; then
  echo "  Цепочка цела. Тишина в чате означает отсутствие проблем."
else
  echo "  Есть поломки — смотрите отмеченные ❌ выше."
fi
