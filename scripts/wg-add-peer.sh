#!/usr/bin/env bash
# Заводит нового клиента и печатает готовый конфиг.
#   ./scripts/wg-add-peer.sh panel-ru 2
#   ./scripts/wg-add-peer.sh nl-1 11
set -euo pipefail

NAME="${1:?имя пира, например panel-ru}"
HOST_ID="${2:?последний октет адреса, например 2}"
DIR=/etc/wireguard
source "$DIR/hub.env"

PEER_IP="${NET}.${HOST_ID}"
umask 077
wg genkey > "$DIR/peers/${NAME}.key"
wg pubkey < "$DIR/peers/${NAME}.key" > "$DIR/peers/${NAME}.pub"

# Добавляем пира в конфиг хаба, если его там ещё нет.
if ! grep -q "# peer: ${NAME}$" "$DIR/wg0.conf"; then
  cat >> "$DIR/wg0.conf" <<CONF

# peer: ${NAME}
[Peer]
PublicKey = $(cat "$DIR/peers/${NAME}.pub")
AllowedIPs = ${PEER_IP}/32
CONF
fi

systemctl restart wg-quick@wg0

CLIENT="$DIR/peers/${NAME}.conf"
cat > "$CLIENT" <<CONF
[Interface]
Address = ${PEER_IP}/24
PrivateKey = $(cat "$DIR/peers/${NAME}.key")

[Peer]
PublicKey = ${HUB_PUBKEY}
Endpoint = ${HUB_ENDPOINT}
# Только сеть мониторинга. Весь остальной трафик сервера идёт как обычно —
# мы не заворачиваем в туннель ничего лишнего.
AllowedIPs = ${NET}.0/24
# Клиенты за NAT: держим канал открытым, иначе хаб до них не достучится.
PersistentKeepalive = 25
CONF

echo "Готово. Адрес ${NAME}: ${PEER_IP}"
echo "Конфиг лежит в ${CLIENT}, содержимое ниже:"
echo "────────────────────────────────────────"
cat "$CLIENT"
