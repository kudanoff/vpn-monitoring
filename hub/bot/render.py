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
    # Для коротких обрывов секунды важны: «1 мин» и «1 мин 50 с» — разное.
    if minutes < 5 and int(seconds) % 60:
        return f"{minutes} мин {int(seconds) % 60} с"
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


# ─────────────────────────────────────────────────────────────────────────
# Аварии в ленте и табло
# ─────────────────────────────────────────────────────────────────────────

def incident(alerts: list[dict], now: float, still: bool = False) -> str:
    """
    Сообщение об аварии. Одна нода — одной строкой с именем; волна нод —
    списком, у каждой своё «сколько лежит». still — напоминание: та же
    авария, но словами «до сих пор», чтобы не принять её за новую.
    """
    if not alerts:
        return ""
    first = alerts[0]
    action = first.get("labels", {}).get("action")
    tail = [f"👉 <i>{action}</i>"] if action else []

    if len(alerts) == 1:
        took = down_for(first, now)
        head = f"{_icon(first)} <b>{_node(first)}</b> — {_kind(first)}"
        when = f"<i>{'до сих пор, уже' if still else 'уже'} {duration(took)}</i>" if took else ""
        return "\n".join([head] + ([when] if when else []) + tail)

    head = f"{_icon(first)} <b>{_kind(first)}</b> — нод: {len(alerts)}"
    lines = [head] + (["<i>до сих пор не поднялись</i>"] if still else []) + [""]
    for a in sorted(alerts, key=_node):
        took = down_for(a, now)
        lines.append(f"  • {_node(a)}" + (f" — {duration(took)}" if took else ""))
    return "\n".join(lines + ([""] + tail if tail else []))


def recovered(alerts: list[dict]) -> str:
    """Авария прошла: что именно было и сколько длилось."""
    if not alerts:
        return ""
    kind = _kind(alerts[0])
    if len(alerts) == 1:
        a = alerts[0]
        took = lasted(a)
        return (f"✅ <b>{_node(a)}</b> — снова в строю\n<i><s>{kind}</s>"
                + (f" · {duration(took)}" if took else "") + "</i>")
    lines = [f"✅ <b>Снова в строю</b> — нод: {len(alerts)}", f"<i><s>{kind}</s></i>", ""]
    for a in sorted(alerts, key=_node):
        took = lasted(a)
        lines.append(f"  • {_node(a)}" + (f" — {duration(took)}" if took else ""))
    return "\n".join(lines)


SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def board(alerts: list[dict], total: int, now: float, stamp: str) -> str:
    """
    Табло: всё, что горит прямо сейчас, одним закреплённым сообщением.
    Сначала аварии, потом предупреждения. Бот правит его, а не пишет новое,
    поэтому отбоев здесь нет: прошедшая проблема просто исчезает из списка.
    """
    if not alerts:
        nodes = f" · нод: {total}" if total else ""
        return f"📌 <b>Всё в порядке</b>{nodes}\n<i>обновлено {stamp}</i>"

    def order(a: dict):
        return (SEVERITY_ORDER.get(a.get("labels", {}).get("severity"), 9), _node(a), _kind(a))

    lines = [f"📌 <b>Сейчас проблем: {len(alerts)}</b>", ""]
    for a in sorted(alerts, key=order):
        node = a.get("labels", {}).get("node")
        took = down_for(a, now)
        lines.append(f"{_icon(a)} " + (f"<b>{node}</b> — " if node else "") + _kind(a)
                     + (f" · {duration(took)}" if took and took >= 60 else ""))
    troubled = {a.get("labels", {}).get("node") for a in alerts if a.get("labels", {}).get("node")}
    if total > len(troubled):
        lines += ["", f"🟢 остальные {total - len(troubled)} в норме"]
    lines += [f"<i>обновлено {stamp}</i>"]
    return "\n".join(lines)


def _node(alert: dict) -> str:
    return alert.get("labels", {}).get("node", "?")


def _icon(alert: dict) -> str:
    summary = alert.get("annotations", {}).get("summary", "").strip()
    return summary.split()[0] if summary else "⚠️"


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


def _pending(alert: dict) -> float:
    """
    Сколько правило выжидало, прежде чем поднять тревогу. vmalert ставит
    началом алерта момент тревоги, а не момент, когда нода пропала, —
    без этой поправки «лежал 1 мин» значило бы «лежал 6 минут».
    """
    try:
        return float(alert.get("annotations", {}).get("pending_seconds", 0))
    except ValueError:
        return 0.0


def down_for(alert: dict, now: float) -> float | None:
    """Сколько проблема длится к этому моменту, секунд."""
    start = _ts(alert.get("startsAt", ""))
    return None if start is None else max(0.0, now - start + _pending(alert))


def lasted(alert: dict) -> float | None:
    """Сколько проблема длилась, секунд, — для отбоя."""
    start, end = _ts(alert.get("startsAt", "")), _ts(alert.get("endsAt", ""))
    if start is None or end is None or end < start:
        return None
    return end - start + _pending(alert)
