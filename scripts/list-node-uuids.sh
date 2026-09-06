#!/usr/bin/env bash
# Печатает UUID нод из метрик панели — заготовку для targets/nodes.yml.
# Запускать на сервере панели:
#   ./scripts/list-node-uuids.sh
set -euo pipefail

ENV_FILE="${REMNAWAVE_ENV:-/opt/remnawave/.env}"
ADDR="${PANEL_METRICS_ADDR:-127.0.0.1:3001}"

USER=$(grep '^METRICS_USER=' "$ENV_FILE" | cut -d= -f2-)
PASS=$(grep '^METRICS_PASS=' "$ENV_FILE" | cut -d= -f2-)
RAW=$(curl -fsS -u "${USER}:${PASS}" "http://${ADDR}/metrics")

# Если панель отдаёт метрику с именем ноды — берём имя оттуда.
if echo "$RAW" | grep -q 'remnawave_node_basic_info'; then
  echo "$RAW" | grep '^remnawave_node_basic_info' | sed -E '
    s/.*node_uuid="([^"]+)".*/\1/;' > /tmp/uuids.txt
  echo "$RAW" | grep '^remnawave_node_basic_info' \
    | sed -E 's/.*node_uuid="([^"]+)".*(node_name|name)="([^"]+)".*/\1 \3/'
  exit 0
fi

echo "# Имён в метриках нет — подставьте их сами, глядя в панель."
echo "# Скопируйте нужные блоки в targets/nodes.yml."
echo
echo "$RAW" | grep '^remnawave_node_online_users' \
  | sed -E 's/.*node_uuid="([^"]+)".*/\1/' | sort -u \
  | while read -r uuid; do
      cat <<BLOCK
- targets: ["10.77.0.XX:9100"]
  labels:
    node: "ПЕРЕИМЕНУЙТЕ"
    node_uuid: "${uuid}"
    public_ip: "БЕЛЫЙ_IP"
    xray_port: "443"
    hoster: ""

BLOCK
    done
