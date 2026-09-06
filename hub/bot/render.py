"""Превращение метрик в текст, который приятно читать с телефона."""


def bps(v: float | None) -> str:
    if v is None:
        return "—"
    for unit, div in (("Гбит/с", 1e9), ("Мбит/с", 1e6), ("Кбит/с", 1e3)):
        if v >= div:
            return f"{v / div:.1f} {unit}"
    return f"{v:.0f} бит/с"


def pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def uptime(seconds: float | None) -> str:
    if not seconds:
        return "—"
    days, rem = divmod(int(seconds), 86400)
    hours = rem // 3600
    return f"{days}д {hours}ч" if days else f"{hours}ч"


def node_icon(state: dict) -> str:
    """Иконка отражает диагноз, а не просто факт недоступности."""
    # Пока агента нет, "up" отсутствует — это не повод считать ноду упавшей.
    if state.get("up") is None:
        if state.get("probe_nl") == 0:
            return "🔴"
        if state.get("probe_ru") == 0:
            return "🚧"
        return "🟢" if state.get("panel") != 0 else "🟡"
    if state.get("up") == 0 and state.get("probe_nl") == 0:
        return "🔴"          # сервер лёг
    if state.get("probe_ru") == 0 and state.get("probe_nl") == 1:
        return "🚧"          # жив, но не виден из РФ
    if state.get("panel") == 0:
        return "🟡"          # панель потеряла ноду, железо в порядке
    if state.get("up") == 0:
        return "⚪️"          # молчит агент мониторинга
    return "🟢"


LEGEND = (
    "🟢 норма  🟡 нет связи с панелью  🚧 не видно из РФ  "
    "🔴 сервер лёг  ⚪️ нет агента\n"
    "📋 — цифры со слов панели, агент на ноде не раскатан"
)


def status_table(nodes: dict[str, dict]) -> str:
    if not nodes:
        return "Пока нет данных. Проверьте, что ноды добавлены в targets/nodes.yml."

    lines = ["<b>Статус нод</b>", ""]
    for name in sorted(nodes):
        s = nodes[name]
        lines.append(
            f"{node_icon(s)} <b>{name}</b>\n"
            f"    ЦП {pct(s.get('cpu'))} · ОЗУ {pct(s.get('mem'))} · "
            f"↑{bps(s.get('tx'))} ↓{bps(s.get('rx'))}"
            + (f" · 👥 {int(s['users'])}" if s.get("users") is not None else "")
            + ("  ·  📋" if s.get("from_panel") else "")
        )
    lines += ["", f"<i>{LEGEND}</i>"]
    return "\n".join(lines)


def node_card(name: str, s: dict) -> str:
    ru = "доступна" if s.get("probe_ru") == 1 else "❗️недоступна"
    nl = "доступна" if s.get("probe_nl") == 1 else "❗️недоступна"
    port = "открыт" if s.get("tcp_ru") == 1 else "❗️закрыт"

    return (
        f"{node_icon(s)} <b>{name}</b>\n"
        f"<i>{s.get('hoster', '—')} · {s.get('public_ip', '—')}</i>\n\n"
        f"<b>Доступность</b>\n"
        f"  из NL: {nl}\n"
        f"  из РФ: {ru}\n"
        f"  порт xray из РФ: {port}\n"
        f"  панель видит ноду: {'да' if s.get('panel') == 1 else 'нет'}\n\n"
        + ("<i>данные со слов панели — агент на ноде не раскатан</i>\n\n"
           if s.get("from_panel") else "")
        + f"<b>Ресурсы</b>\n"
        f"  ЦП: {pct(s.get('cpu'))}   steal: {pct(s.get('steal'))}\n"
        f"  ОЗУ: {pct(s.get('mem'))}\n"
        f"  аптайм: {uptime(s.get('uptime'))}\n\n"
        f"<b>Канал</b>\n"
        f"  сейчас: ↑{bps(s.get('tx'))} ↓{bps(s.get('rx'))}\n"
        f"  p95 за сутки: {bps(s.get('p95'))}\n"
        f"  скорость линка: {bps(s.get('link'))}\n"
        f"  последний замер iperf3: {bps(s.get('speedtest'))}\n"
    )


def alert_message(alert: dict) -> str:
    labels = alert.get("labels", {})
    ann = alert.get("annotations", {})
    resolved = alert.get("status") == "resolved"

    head = "✅ <b>Восстановлено</b>" if resolved else ann.get("summary", labels.get("alertname", "Алерт"))
    body = [head]

    if resolved:
        body.append(ann.get("summary", labels.get("alertname", "")))
    else:
        if ann.get("description"):
            body.append(ann["description"])
        if labels.get("action"):
            body.append(f"\n👉 <i>{labels['action']}</i>")

    return "\n".join(body)
