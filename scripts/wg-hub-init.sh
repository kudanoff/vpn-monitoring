#!/usr/bin/env bash
# Поднимает WireGuard-сервер на хабе. Запускать один раз.
#   ./scripts/wg-hub-init.sh <публичный_IP_хаба> [порт]
set -euo pipefail

PUBLIC_IP="${1:?укажите публичный IP хаба}"
PORT="${2:-51820}"
NET="10.77.0"
DIR=/etc/wireguard

command -v wg >/dev/null || { apt-get update -qq && apt-get install -y -qq wireguard-tools; }
mkdir -p "$DIR/peers"
chmod 700 "$DIR"

if [[ ! -f "$DIR/hub.key" ]]; then
  umask 077
  wg genkey > "$DIR/hub.key"
  wg pubkey < "$DIR/hub.key" > "$DIR/hub.pub"
fi

cat > "$DIR/wg0.conf" <<CONF
[Interface]
Address = ${NET}.1/24
ListenPort = ${PORT}
PrivateKey = $(cat "$DIR/hub.key")
SaveConfig = false
CONF

# Параметры хаба сохраняем рядом — их читает скрипт добавления пиров.
cat > "$DIR/hub.env" <<CONF
HUB_ENDPOINT=${PUBLIC_IP}:${PORT}
HUB_PUBKEY=$(cat "$DIR/hub.pub")
NET=${NET}
CONF

# Форвардинг нужен, чтобы пиры видели друг друга через хаб.
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-wg-forward.conf
sysctl -q -p /etc/sysctl.d/99-wg-forward.conf

systemctl enable --now wg-quick@wg0
systemctl restart wg-quick@wg0

echo
echo "WireGuard поднят."
echo "  адрес хаба в приватной сети: ${NET}.1"
echo "  endpoint для клиентов:       ${PUBLIC_IP}:${PORT}"
echo
echo "ВАЖНО: откройте UDP ${PORT} в фаерволе хаба."
