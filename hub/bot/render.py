"""Превращение метрик в текст, который приятно читать с телефона."""
import datetime
import re


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


def _line(name: str, s: dict) -> str:
    return (
        f"{node_icon(s)} {s.get('flag', '')} <b>{name}</b>\n"
        f"    ЦП {pct(s.get('cpu'))} · ОЗУ {pct(s.get('mem'))} · "
        f"↑{bps(s.get('tx'))} ↓{bps(s.get('rx'))}"
        + (f" · 👥 {int(s['users'])}" if s.get("users") is not None else "")
        + ("  ·  📋" if s.get("from_panel") else "")
    )


def status_summary(nodes: dict[str, dict]) -> str:
    """
    Главный экран. Отвечает на вопрос "всё ли в порядке" за одну секунду:
    сначала счётчики, потом только проблемные ноды. Сорок строк зелёного
    листать никто не будет, а значит и проблему в них не заметит.
    """
    if not nodes:
        return "Пока нет данных. Проверьте, что метрики панели доезжают."

    # Нераскатанный агент — не авария, а незаконченный второй этап.
    # В список проблем такое не попадает, иначе весь парк выглядит сломанным.
    problems = {n: s for n, s in nodes.items() if node_icon(s) not in ("🟢", "⚪️")}
    no_agent = sum(1 for s in nodes.values() if node_icon(s) == "⚪️")
    ok_count = len(nodes) - len(problems)

    lines = [f"<b>Ноды: {len(nodes)}</b>", ""]

    users = sum(int(s["users"]) for s in nodes.values() if s.get("users"))
    traffic = sum(s.get("tx") or 0 for s in nodes.values())
    lines.append(f"🟢 в норме: {ok_count}    👥 онлайн: {users}    ↑ всего: {bps(traffic)}")
    if no_agent:
        lines.append(f"<i>без агента: {no_agent} — метрики со слов панели</i>")

    if not problems:
        lines += ["", "Проблем нет."]
    else:
        lines += ["", f"<b>Требуют внимания — {len(problems)}</b>", ""]
        # Сначала то, что горит ярче: упавшие, потом блокировки, потом остальное.
        order = {"🔴": 0, "🚧": 1, "🟡": 2}
        for name in sorted(problems, key=lambda n: (order.get(node_icon(problems[n]), 9), n)):
            lines.append(_line(name, problems[name]))

    # Самые нагруженные — чтобы видеть, где горит канал, не открывая графики.
    top = sorted(nodes.items(), key=lambda kv: kv[1].get("tx") or 0, reverse=True)[:5]
    if top and (top[0][1].get("tx") or 0) > 0:
        lines += ["", "<b>Больше всего трафика</b>"]
        for name, s in top:
            lines.append(f"  {s.get('flag', '')} {name} — ↑{bps(s.get('tx'))} · 👥 {int(s.get('users') or 0)}")

    lines += ["", f"<i>{LEGEND}</i>"]
    return "\n".join(lines)


def status_table(nodes: dict[str, dict]) -> str:
    """Полный список — по кнопке, когда действительно нужен весь парк."""
    if not nodes:
        return "Пока нет данных."
    lines = ["<b>Все ноды</b>", ""]
    for name in sorted(nodes):
        lines.append(_line(name, nodes[name]))
    lines += ["", f"<i>{LEGEND}</i>"]
    return "\n".join(lines)


def node_card(name: str, s: dict) -> str:
    ru = "доступна" if s.get("probe_ru") == 1 else "❗️недоступна"
    nl = "доступна" if s.get("probe_nl") == 1 else "❗️недоступна"
    port = "открыт" if s.get("tcp_ru") == 1 else "❗️закрыт"

    return (
        f"{node_icon(s)} {s.get('flag', '')} <b>{name}</b>\n"
        f"<i>{s.get('provider') or s.get('hoster') or '—'}"
        f"{' · ' + s['public_ip'] if s.get('public_ip') else ''}</i>\n\n"
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


def duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} с"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {rest} мин" if rest else f"{hours} ч"
    days, rest_h = divmod(hours, 24)
    return f"{days} д {rest_h} ч"


def node_not_found(query: str, names: list[str]) -> str:
    """Промах по имени. Показываем, из чего выбирать, — искать самому негде."""
    if not names:
        return f"Нода <b>{query}</b> не найдена, и списка нод сейчас нет."
    head = "Похоже на" if len(names) < 10 else "Есть ноды"
    listed = "\n".join(f"  • <code>{'&quot;' + n + '&quot;' if ' ' in n else n}</code>" for n in names[:20])
    tail = "\n  …" if len(names) > 20 else ""
    return (f"Нода <b>{query}</b> не найдена.\n{head}:\n{listed}{tail}\n\n"
            "<i>Имя с пробелами берите в кавычки.</i>")


def mute_list(rows: list[dict], kinds: dict[str, tuple[str, str]]) -> str:
    """Что сейчас молчит и сколько ещё будет молчать."""
    if not rows:
        return "🔔 Заглушек нет, слышно всё."
    lines = ["<b>Заглушено</b>", ""]
    for r in sorted(rows, key=lambda x: (x["kind"], x["node"] or "")):
        icon, what = kinds.get(r["kind"], ("🔇", "молчит"))
        if r.get("alert"):
            what = f"{what} {r['alert']}"
        by = f" · {r['by']}" if r.get("by") else ""
        lines.append(f"{icon} <b>{r['node']}</b> — {what}\n"
                     f"    ещё {duration(r['left'])}{by}")
    lines += ["", "<i>Снять: /unmute &lt;имя&gt;</i>"]
    return "\n".join(lines)


def alert_group(payload: dict, since: float | None = None, back: list[dict] | None = None) -> str:
    """
    Карточка проблемы: одно сообщение на группу алертов. Двадцать четыре
    письма про одно и то же событие читать никто не станет, а значит и аварию
    в них не разглядит. back — ноды из этой группы, которые уже поднялись:
    они остаются в карточке зачёркнутыми, чтобы было видно, что прошло.
    """
    alerts = payload.get("alerts", [])
    if not alerts:
        return ""
    if len(alerts) == 1 and not back:
        text = alert_message(alerts[0])
        return text + (f"\n<i>длится {duration(since)}</i>" if since else "")

    first = alerts[0]
    name = first.get("annotations", {}).get("summary", "")
    icon = name.split()[0] if name.strip() else "⚠️"

    lines = [f"{icon} <b>{_kind(first)}</b> — нод: {len(alerts)}", ""]
    for a in sorted(alerts, key=_node):
        hoster = a.get("labels", {}).get("hoster")
        lines.append(f"  • {_node(a)}" + (f" <i>({hoster})</i>" if hoster else ""))
    for a in sorted(back or [], key=_node):
        took = lasted(a)
        lines.append(f"  ✅ <s>{_node(a)}</s>" + (f" — {duration(took)}" if took is not None else ""))

    action = first.get("labels", {}).get("action")
    if action:
        lines += ["", f"👉 <i>{action}</i>"]
    if since:
        lines += [f"<i>длится {duration(since)}</i>"]

    return "\n".join(lines)


def alert_message(alert: dict) -> str:
    labels = alert.get("labels", {})
    ann = alert.get("annotations", {})
    body = [ann.get("summary", labels.get("alertname", "Алерт"))]
    if ann.get("description"):
        body.append(ann["description"])
    if labels.get("action"):
        body.append(f"\n👉 <i>{labels['action']}</i>")
    return "\n".join(body)


def alert_resolved(alerts: list[dict]) -> str:
    """
    Отбой: во что превращается карточка, когда проблема прошла. Проблема
    зачёркнута, рядом — сколько длилась: по одной строке понятно, было это
    мгновенное моргание или авария.
    """
    if not alerts:
        return ""
    kind = _kind(alerts[0])
    if len(alerts) == 1:
        a = alerts[0]
        node = f"<b>{_node(a)}</b> — " if a.get("labels", {}).get("node") else ""
        took = lasted(a)
        return (f"✅ {node}отбой\n<s>{kind}</s>"
                + (f" · длилось {duration(took)}" if took is not None else ""))

    lines = [f"✅ <b>Отбой</b> — <s>{kind}</s>", ""]
    for a in sorted(alerts, key=_node):
        took = lasted(a)
        lines.append(f"  • {_node(a)}" + (f" — {duration(took)}" if took is not None else ""))
    return "\n".join(lines)


def _node(alert: dict) -> str:
    return alert.get("labels", {}).get("node", "?")


def _kind(alert: dict) -> str:
    """Суть алерта без иконки, проекта и ноды: «сервер недоступен»."""
    summary = alert.get("annotations", {}).get("summary", "")
    if "—" in summary:
        return summary.split("—", 1)[1].strip()
    # Алерт не про ноду: срезаем только иконку в начале.
    return re.sub(r"^[^\w\[]+", "", summary) or alert.get("labels", {}).get("alertname", "Алерт")


def _ts(value: str) -> float | None:
    # Alertmanager пишет время с наносекундами, Python их не разбирает —
    # дробная часть для «длилось N минут» всё равно не нужна.
    if not value or value.startswith("0001-"):
        return None
    try:
        return datetime.datetime.fromisoformat(re.sub(r"\.\d+", "", value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def lasted(alert: dict) -> float | None:
    """Сколько алерт горел, секунд."""
    start, end = _ts(alert.get("startsAt", "")), _ts(alert.get("endsAt", ""))
    if start is None or end is None or end < start:
        return None
    return end - start
