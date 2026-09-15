"""
Бот-витрина мониторинга.

Он ничего не собирает и ничего не решает: метрики лежат в VictoriaMetrics,
логику "когда шуметь" держит Alertmanager. Бот только показывает и пересылает.
Поэтому его можно спокойно перезапускать в любой момент — состояния в нём нет.
"""
import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import time

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import render
from vm import by_node, instant

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
ADMINS = {int(x) for x in os.getenv("TELEGRAM_ADMINS", "").replace(" ", "").split(",") if x}
WEBHOOK_SECRET = os.getenv("REMNAWAVE_WEBHOOK_SECRET", "")
# Ноды, которые не нужно показывать и по которым не нужно алертить:
# архивные, тестовые, служебные. Регулярное выражение по имени из панели.
IGNORE_RE = re.compile(os.getenv("NODES_IGNORE", r"archive|ipcheker"), re.IGNORECASE)
# Один хаб обслуживает несколько проектов. Бот показывает только свой:
# у каждого проекта свой бот, свой чат и свои дежурные.
PROJECT = os.getenv("PROJECT", "")
# Нарастающие паузы между напоминаниями, в минутах. Первое сообщение уходит
# сразу, дальше по этому списку; когда он кончится, повторяется последнее
# значение. Alertmanager так не умеет — у него интервал фиксированный, —
# поэтому он будит бота ежеминутно, а бот решает, пора ли писать.
REPEAT_STEPS = [int(x) for x in os.getenv("REPEAT_STEPS", "3,5,8,10,30,60").split(",") if x.strip()]
ALERTMANAGER_URL = os.getenv("ALERTMANAGER_URL", "http://alertmanager:9093")
HEALTHCHECKS_URL = os.getenv("HEALTHCHECKS_URL", "")

# Два вида тишины. Полная — когда нода выведена из строя намеренно и про неё
# не нужно вообще ничего. Лёгкая — когда нода работает, но её связь с панелью
# рвётся: про это уже известно, чинить пока нечем, а всё остальное по ноде
# (лёг сервер, кончается диск) должно доходить как обычно.
LINK_ALERTS = ("NodeFlapping", "PanelNodeDisconnected", "PanelLostNodeButHostAlive")
# Вебхуки панели идут мимо Alertmanager, поэтому их приходится глушить в боте.
LINK_EVENTS = {"node.connection_lost", "node.connection_restored"}
MUTE_KINDS = {
    "full": ("🔇", "молчит всё"),
    "link": ("🔀", "молчат только разрывы связи"),
    # Заглушка, поставленная руками через amtool на какой-то другой алерт.
    "other": ("🔕", "молчит часть алертов"),
}
DEFAULT_HOURS = {"full": 2.0, "link": 24.0 * 7}

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ─────────────────────────────────────────────────────────────────────────
# Сбор картины по всем нодам
# ─────────────────────────────────────────────────────────────────────────

# Панель и агент дополняют друг друга: панель отдаёт данные по всем нодам
# сразу, агент — точнее и независимо. Где есть агент, берём его; где нет,
# показываем то, что знает панель. Поэтому у большинства полей два запроса.
def _p(inner: str = "") -> str:
    """Селектор проекта для PromQL: добавляется к каждому запросу бота."""
    if not PROJECT:
        return "{" + inner + "}" if inner else ""
    sel = f'project="{PROJECT}"'
    return "{" + (f"{inner}, {sel}" if inner else sel) + "}"


# Панель и агент дополняют друг друга: панель отдаёт данные по всем нодам
# сразу, агент — точнее и независимо. Где есть агент, берём его; где нет,
# показываем то, что знает панель. Поэтому у большинства полей два запроса.
QUERIES = {
    "up":        f'max by(node) (up{_p("job=~\"node.*\"")})',
    "probe_nl":  f'max by(node) (probe_success{_p("vantage=\"nl\"")})',
    "probe_ru":  f'max by(node) (probe_success{_p("job=~\"probe_ru_.*\", vantage=\"ru\"")})',
    "tcp_ru":    f'max by(node) (probe_success{_p("job=~\"probe_ru_tcp.*\"")})',
    "panel":     f"max by(node) (node:panel_status{_p()})",
    "users":     f"max by(node) (node:panel_users{_p()})",
    # Агент, если раскатан
    "cpu":       f"max by(node) (node:cpu_busy:ratio{_p()})",
    "mem":       f"max by(node) (node:mem_used:ratio{_p()})",
    "rx":        f"max by(node) (node:net_rx:bps{_p()})",
    "tx":        f"max by(node) (node:net_tx:bps{_p()})",
    # Панель — источник по умолчанию
    "cpu_panel": f"max by(node) (node:panel_cpu{_p()})",
    "mem_panel": f"max by(node) (node:panel_mem{_p()})",
    "rx_panel":  f"max by(node) (node:panel_rx{_p()})",
    "tx_panel":  f"max by(node) (node:panel_tx{_p()})",
}

DETAIL_QUERIES = {
    "steal":        f'avg by(node) (rate(node_cpu_seconds_total{_p("mode=\"steal\"")}[10m]))',
    "uptime":       f"max by(node) (time() - node_boot_time_seconds{_p()})",
    "uptime_panel": f"max by(node) (node:panel_uptime{_p()})",
    "p95":          f"max by(node) (node:net_tx:p95_24h{_p()})",
    "p95_panel":    f"max by(node) (node:panel_tx_p95_24h{_p()})",
    "link":         f"max by(node) (node:link_capacity:bps{_p()})",
    "speedtest":    f"max by(node) (node_port_speedtest_bps{_p()})",
}

# Что чем подменяется, если агента на ноде ещё нет.
FALLBACKS = {
    "cpu": "cpu_panel",
    "mem": "mem_panel",
    "rx": "rx_panel",
    "tx": "tx_panel",
    "uptime": "uptime_panel",
    "p95": "p95_panel",
}


async def collect(session: aiohttp.ClientSession, detailed: bool = False) -> dict[str, dict]:
    queries = {**QUERIES, **DETAIL_QUERIES} if detailed else QUERIES
    results = await asyncio.gather(
        *(by_node(session, expr) for expr in queries.values()), return_exceptions=True
    )

    nodes: dict[str, dict] = {}
    for key, result in zip(queries, results):
        if isinstance(result, Exception):
            log.warning("запрос %s не удался: %s", key, result)
            continue
        for name, value in result.items():
            nodes.setdefault(name, {})[key] = value

    # Где агента нет — показываем цифры панели, но помечаем это,
    # чтобы по боту было видно, какие ноды ещё не раскатаны.
    for state in nodes.values():
        for field, source in FALLBACKS.items():
            if state.get(field) is None and state.get(source) is not None:
                state[field] = state[source]
                state.setdefault("from_panel", set()).add(field)

    # Отсекаем архивные и служебные ноды: они всегда "отключены" и,
    # если их не убрать, сводка на две трети состоит из ложных тревог.
    for name in [n for n in nodes if IGNORE_RE.search(n)]:
        del nodes[name]

    # Флаг страны и провайдер приходят от панели — подписываем ими карточки.
    try:
        for row in await instant(session, f"node:name_map{_p()}"):
            if IGNORE_RE.search(row["node"]) or row["node"] not in nodes:
                continue
            meta = nodes[row["node"]]
            meta["flag"] = row.get("node_country_emoji", "")
            meta["provider"] = row.get("provider_name", "")
    except Exception as exc:
        log.warning("не удалось получить имена нод: %s", exc)

    # Ноды, которые есть в конфиге, но ещё ни разу не отдали метрик,
    # всё равно должны быть видны — иначе пропажу легко не заметить.
    for row in await instant(session, f'up{_p("job=~\"node.*\"")}'):
        meta = nodes.setdefault(row.get("node", "?"), {})
        meta.setdefault("hoster", row.get("hoster"))
        meta.setdefault("public_ip", row.get("public_ip"))

    return nodes


# ─────────────────────────────────────────────────────────────────────────
# Команды
# ─────────────────────────────────────────────────────────────────────────

def allowed(message: Message) -> bool:
    return not ADMINS or message.from_user.id in ADMINS


def kb(full: bool = False) -> InlineKeyboardMarkup:
    other = ("📋 Кратко", "refresh") if full else ("📋 Все ноды", "all")
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data="all" if full else "refresh"),
        InlineKeyboardButton(text=other[0], callback_data=other[1]),
    ]])


@dp.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Команды</b>\n"
        "/status — сводка по всем нодам\n"
        "/node &lt;имя&gt; — подробности по ноде\n"
        "/chatid — ID этого чата, для настройки алертов\n\n"
        "<b>Тишина</b>\n"
        "/mute &lt;имя&gt; [срок] — заглушить по ноде всё\n"
        "/mutelink &lt;имя&gt; [срок] — заглушить только разрывы связи\n"
        "/mutes — что сейчас заглушено\n"
        "/unmute &lt;имя&gt; — вернуть звук\n\n"
        "<i>Срок: 30м, 2ч, 7д, 2н; по умолчанию 2 ч и 7 д соответственно.\n"
        "Имя с пробелами — в кавычках: /mute \"DE Frankfurt 1\" 3ч</i>\n\n"
        "<i>Архивные и служебные ноды скрыты. Список задаётся "
        "переменной NODES_IGNORE в .env</i>"
    )


@dp.message(Command("chatid"))
async def cmd_chatid(message: Message) -> None:
    """
    Идентификатор чата, где выполнена команда. Нужен для TELEGRAM_CHAT_ID:
    у группы он меняется при превращении в супергруппу, и алерты молча
    перестают доходить, хотя команды продолжают работать.
    """
    chat = message.chat
    current = "совпадает с настройкой" if chat.id == CHAT_ID else f"НЕ совпадает (в настройке {CHAT_ID})"
    await message.answer(
        f"Чат: <b>{chat.title or chat.full_name}</b>\n"
        f"Тип: {chat.type}\n"
        f"ID: <code>{chat.id}</code>\n\n"
        f"<i>{current}</i>"
    )


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not allowed(message):
        return
    async with aiohttp.ClientSession() as session:
        nodes = await collect(session)
    await message.answer(render.status_summary(nodes), reply_markup=kb())


@dp.callback_query(F.data.in_({"refresh", "all"}))
async def cb_view(call: CallbackQuery) -> None:
    full = call.data == "all"
    async with aiohttp.ClientSession() as session:
        nodes = await collect(session)
    body = render.status_table(nodes) if full else render.status_summary(nodes)
    text = body + f"\n<i>обновлено {time.strftime('%H:%M:%S')}</i>"
    try:
        await call.message.edit_text(text, reply_markup=kb(full))
    except Exception:
        pass  # Telegram ругается, если текст не изменился — это нормально
    await call.answer()


@dp.message(Command("node"))
async def cmd_node(message: Message, command: CommandObject) -> None:
    if not allowed(message):
        return
    name = (command.args or "").strip()
    if not name:
        await message.answer("Укажите имя ноды: <code>/node nl-1</code>")
        return

    async with aiohttp.ClientSession() as session:
        nodes = await collect(session, detailed=True)

    if name not in nodes:
        known = ", ".join(sorted(nodes)) or "нет данных"
        await message.answer(f"Нода <b>{name}</b> не найдена.\nЕсть: {known}")
        return

    await message.answer(render.node_card(name, nodes[name]))


# ─────────────────────────────────────────────────────────────────────────
# Заглушки
# ─────────────────────────────────────────────────────────────────────────

def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def _matchers(node: str, kind: str) -> list[dict]:
    m = [{"name": "node", "value": node, "isRegex": False, "isEqual": True}]
    # Alertmanager один на все проекты, а имена нод в разных панелях
    # совпадают. Без этого матчера заглушка в одном проекте затыкает
    # одноимённую ноду в соседнем.
    if PROJECT:
        m.append({"name": "project", "value": PROJECT, "isRegex": False, "isEqual": True})
    if kind == "link":
        m.append({"name": "alertname", "value": "|".join(LINK_ALERTS),
                  "isRegex": True, "isEqual": True})
    return m


async def fetch_mutes(session: aiohttp.ClientSession) -> list[dict]:
    """Действующие заглушки по нодам нашего проекта."""
    async with session.get(f"{ALERTMANAGER_URL}/api/v2/silences") as r:
        items = await r.json()
    return [s for s in items
            if s.get("status", {}).get("state") == "active" and mute_node(s)]


def mute_node(silence: dict) -> str | None:
    """Имя ноды из заглушки — или None, если заглушка не про ноду и не наша."""
    node = None
    for m in silence.get("matchers", []):
        if m.get("name") == "project" and PROJECT and m.get("value") != PROJECT:
            return None
        if m.get("name") == "node" and not m.get("isRegex"):
            node = m.get("value")
    return node


def mute_kind(silence: dict) -> str:
    """
    Заглушка без матчера по alertname глушит по ноде всё — это full.
    Если матчер есть и в нём наши «связные» алерты — это link. Прочие
    ручные заглушки из amtool попадают в other и вебхуков не касаются.
    """
    for m in silence.get("matchers", []):
        if m.get("name") != "alertname":
            continue
        return "link" if any(a in (m.get("value") or "") for a in LINK_ALERTS) else "other"
    return "full"


# Список заглушек нужен на каждый вебхук от панели, а меняется он редко —
# поэтому держим копию и обновляем раз в полминуты.
_mute_cache: dict = {"at": 0.0, "items": []}


async def muted(session: aiohttp.ClientSession, node: str, kind: str) -> bool:
    """Заглушена ли нода для события этого рода: link — про связь, other — всё прочее."""
    now = time.time()
    if now - _mute_cache["at"] > 30:
        try:
            _mute_cache["items"] = await fetch_mutes(session)
            _mute_cache["at"] = now
        except Exception as exc:
            # Молчать из-за недоступного Alertmanager хуже, чем лишний раз шумнуть.
            log.warning("не удалось получить заглушки: %s", exc)
            return False
    return any(mute_node(s) == node and mute_kind(s) in (("full", "link") if kind == "link" else ("full",))
               for s in _mute_cache["items"])


# ── разбор аргументов ────────────────────────────────────────────────────

_UNITS = {"м": 1 / 60, "мин": 1 / 60, "m": 1 / 60, "ч": 1, "h": 1,
          "д": 24, "дн": 24, "d": 24, "н": 168, "нед": 168, "w": 168}
_DURATION_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(мин|дн|нед|[мчднmhdw])?$", re.IGNORECASE)
# Телефон норовит поставить «ёлочки» и «лапки» вместо простых кавычек.
_QUOTES = "\"'«»“”„`"


def parse_hours(token: str) -> float | None:
    """«30м», «2ч», «7д», «2н», «12» (часы) — в часы. Иначе None."""
    m = _DURATION_RE.match(token.strip())
    if not m:
        return None
    return float(m.group(1).replace(",", ".")) * _UNITS.get((m.group(2) or "ч").lower(), 1)


def readings(args: str) -> list[tuple[str, float | None]]:
    """
    Разбирает «имя [срок]». Имена нод в панели бывают с пробелами
    («DE Frankfurt 1»), поэтому однозначно разделить имя и срок нельзя:
    возвращаем все разумные прочтения, а выбирает из них тот, кто знает
    список нод. Кавычки снимают неоднозначность сразу.
    """
    args = args.strip()
    if not args:
        return []
    if args[0] in _QUOTES:
        end = next((i for i in range(1, len(args)) if args[i] in _QUOTES), -1)
        if end > 0:
            return [(args[1:end].strip(), parse_hours(args[end + 1:]))]
    # Первым идёт прочтение «всё это — имя»: нода «DE Frankfurt 1» должна
    # опознаваться и без кавычек, а не превращаться в мут на один час.
    out: list[tuple[str, float | None]] = [(args, None)]
    head, _, tail = args.rpartition(" ")
    if head and parse_hours(tail):
        out.append((head.strip(), parse_hours(tail)))
    return out


def match_node(query: str, names: list[str], exact: bool = False) -> list[str]:
    q = query.strip().lower()
    if not q:
        return []
    hit = [n for n in names if n.lower() == q]
    if hit or exact:
        return hit[:1]
    return [n for n in names if q in n.lower()]


async def known_nodes(session: aiohttp.ClientSession) -> list[str]:
    """Имена нод — без тяжёлой сводки: для заглушек нужны только они."""
    names: set[str] = set()
    for expr in (f"node:panel_status{_p()}", f'up{_p("job=~\"node.*\"")}'):
        try:
            rows = await instant(session, expr)
        except Exception as exc:
            log.warning("не удалось получить список нод: %s", exc)
            continue
        names |= {r["node"] for r in rows if r.get("node") and not IGNORE_RE.search(r["node"])}
    return sorted(names)


async def resolve(session: aiohttp.ClientSession, args: str) -> tuple[str | None, float | None, list[str]]:
    """Имя из сообщения → настоящее имя ноды, срок и подсказка при промахе."""
    names = await known_nodes(session)
    options = readings(args)
    similar: list[str] = []
    # Сначала ищем точное совпадение по всем прочтениям и только потом
    # по кусочку имени: «/mute nl-1 24» — это нода nl-1 на сутки, даже если
    # «nl-1 24» похоже на начало имени какой-нибудь другой ноды.
    for exact in (True, False):
        for query, hours in options:
            found = match_node(query, names, exact)
            if len(found) == 1:
                return found[0], hours, []
            similar = similar or found
    return None, None, similar or names


# ── команды ──────────────────────────────────────────────────────────────

async def do_mute(message: Message, args: str, kind: str) -> None:
    icon, what = MUTE_KINDS[kind]
    if not args.strip():
        await message.answer(
            f"{icon} <b>{what}</b>\n"
            f"Пример: <code>/{'mutelink' if kind == 'link' else 'mute'} nl-1 12ч</code>\n"
            f"Срок: <code>30м</code>, <code>2ч</code>, <code>7д</code>, <code>2н</code>. "
            f"По умолчанию {render.duration(DEFAULT_HOURS[kind] * 3600)}.\n"
            f"Имя с пробелами — в кавычках: <code>/mute \"DE Frankfurt 1\" 3ч</code>"
        )
        return

    async with aiohttp.ClientSession() as session:
        node, hours, hint = await resolve(session, args)
        if not node:
            await message.answer(render.node_not_found(args, hint))
            return
        hours = hours or DEFAULT_HOURS[kind]
        now = time.time()
        payload = {
            "matchers": _matchers(node, kind),
            "startsAt": _fmt_ts(now),
            "endsAt": _fmt_ts(now + hours * 3600),
            "createdBy": message.from_user.username or str(message.from_user.id),
            "comment": f"mute:{kind} — заглушено из телеграма",
        }
        async with session.post(f"{ALERTMANAGER_URL}/api/v2/silences", json=payload) as r:
            ok, body = r.status < 300, await r.text()

    if not ok:
        await message.answer(f"Не вышло заглушить: {body}")
        return

    _mute_cache["at"] = 0.0  # чтобы вебхуки панели замолчали сразу
    try:
        sid = json.loads(body).get("silenceID", "")
    except ValueError:
        sid = ""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔔 Вернуть звук", callback_data=f"unmute:{sid}")
    ]]) if sid else None
    tail = ("Остальное по ноде — упавший сервер, диск, блокировка из РФ — приходит как обычно."
            if kind == "link" else "Про эту ноду не придёт ничего, включая аварии.")
    await message.answer(
        f"{icon} <b>{node}</b> — {what} на {render.duration(hours * 3600)}\n<i>{tail}</i>",
        reply_markup=keyboard,
    )


@dp.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject) -> None:
    """Полная тишина по ноде: плановые работы, отключённый сервер."""
    if allowed(message):
        await do_mute(message, command.args or "", "full")


@dp.message(Command("mutelink"))
async def cmd_mutelink(message: Message, command: CommandObject) -> None:
    """Тишина только про связь: нода флапает, руки дойдут потом."""
    if allowed(message):
        await do_mute(message, command.args or "", "link")


@dp.message(Command("mutes", "muted"))
async def cmd_mutes(message: Message) -> None:
    if not allowed(message):
        return
    async with aiohttp.ClientSession() as session:
        try:
            items = await fetch_mutes(session)
        except Exception as exc:
            await message.answer(f"Alertmanager не ответил: {exc}")
            return
    rows = [{
        "node": mute_node(s),
        "kind": mute_kind(s),
        "left": _left(s),
        "by": s.get("createdBy", ""),
    } for s in items]
    await message.answer(render.mute_list(rows, MUTE_KINDS))


def _left(silence: dict) -> float:
    """Сколько заглушке осталось, секунд."""
    try:
        ends = datetime.datetime.fromisoformat(silence["endsAt"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return 0.0
    return max(0.0, ends.timestamp() - time.time())


async def drop_mutes(session: aiohttp.ClientSession, node: str | None = None,
                     silence_id: str | None = None) -> int:
    removed = 0
    for s in await fetch_mutes(session):
        if silence_id and s.get("id") != silence_id:
            continue
        if node and mute_node(s) != node:
            continue
        async with session.delete(f"{ALERTMANAGER_URL}/api/v2/silences/{s['id']}") as r:
            removed += r.status < 300
    if removed:
        _mute_cache["at"] = 0.0
    return removed


@dp.message(Command("unmute"))
async def cmd_unmute(message: Message, command: CommandObject) -> None:
    """Снимает по ноде обе заглушки разом — и полную, и по связи."""
    if not allowed(message):
        return
    args = (command.args or "").strip()
    if not args:
        await message.answer("Пример: <code>/unmute nl-1</code>. Что заглушено — <code>/mutes</code>")
        return
    async with aiohttp.ClientSession() as session:
        node, _, hint = await resolve(session, args)
        if not node:
            await message.answer(render.node_not_found(args, hint))
            return
        removed = await drop_mutes(session, node=node)
    await message.answer(
        f"🔔 <b>{node}</b> — звук вернулся" if removed else f"У <b>{node}</b> и так не было заглушек"
    )


@dp.callback_query(F.data.startswith("unmute:"))
async def cb_unmute(call: CallbackQuery) -> None:
    if ADMINS and call.from_user.id not in ADMINS:
        await call.answer("Нет прав", show_alert=True)
        return
    async with aiohttp.ClientSession() as session:
        removed = await drop_mutes(session, silence_id=call.data.split(":", 1)[1])
    await call.answer("Звук вернулся" if removed else "Заглушка уже снята")
    try:
        await call.message.edit_text("🔔 Заглушка снята", reply_markup=None)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────
# Входящие вебхуки
# ─────────────────────────────────────────────────────────────────────────

# Состояние эскалации по группам алертов. В памяти: после перезапуска бота
# отсчёт начнётся заново, и это правильнее, чем молчать из-за потерянного
# состояния.
_escalation: dict[str, dict] = {}


async def _send(text: str) -> None:
    try:
        await bot.send_message(CHAT_ID, text)
    except Exception as exc:
        log.error("не отправился алерт: %s", exc)


async def handle_alerts(request: web.Request) -> web.Response:
    """
    Алерты от Alertmanager. Он стучится часто, а решение "напоминать или
    промолчать" принимается здесь — по нарастающим паузам REPEAT_STEPS.
    """
    payload = await request.json()
    alerts = payload.get("alerts", [])
    firing = [a for a in alerts if a.get("status") == "firing"]
    key = payload.get("groupKey") or str(sorted(payload.get("groupLabels", {}).items()))
    now = time.time()

    if not firing:
        # Проблема ушла: сообщаем и забываем историю напоминаний, чтобы
        # следующая авария начиналась с чистого листа.
        _escalation.pop(key, None)
        text = render.alert_group(payload)
        if text:
            await _send(text)
        return web.Response(text="ok")

    # Состав группы — часть опознавательного знака: если к аварии добавилась
    # ещё одна нода, это новая ситуация, а не продолжение старой.
    sig = tuple(sorted(a.get("fingerprint", "") for a in firing))
    state = _escalation.get(key)

    if state is None or state["sig"] != sig:
        _escalation[key] = {"sig": sig, "since": now, "last": now, "step": 0}
        await _send(render.alert_group(payload))
        return web.Response(text="ok")

    pause = REPEAT_STEPS[min(state["step"], len(REPEAT_STEPS) - 1)] * 60
    if now - state["last"] < pause:
        return web.Response(text="ok")  # ещё рано напоминать

    state["last"] = now
    state["step"] += 1
    await _send(render.alert_group(payload, since=now - state["since"]))
    return web.Response(text="ok")


async def handle_watchdog(request: web.Request) -> web.Response:
    """
    Сторож. Сам по себе в чат не пишет — вместо этого пингует внешний сервис.
    Если хаб умрёт целиком, пинги прекратятся и внешний сервис напишет вам сам.
    Тишина в чате перестаёт означать "всё хорошо".
    """
    if HEALTHCHECKS_URL:
        try:
            async with aiohttp.ClientSession() as session:
                await session.get(HEALTHCHECKS_URL, timeout=aiohttp.ClientTimeout(total=10))
        except Exception as exc:
            log.warning("сторож не достучался наружу: %s", exc)
    return web.Response(text="ok")


async def handle_remnawave(request: web.Request) -> web.Response:
    """
    Вебхуки от панели. Приходят мгновенно, не дожидаясь опроса, —
    это самый быстрый канал про "нода отвалилась".
    """
    raw = await request.read()

    if WEBHOOK_SECRET:
        signature = request.headers.get("X-Remnawave-Signature", "")
        expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            log.warning("вебхук с неверной подписью отброшен")
            return web.Response(status=401, text="bad signature")

    event = json.loads(raw)
    name = event.get("event", "")
    data = event.get("data", {}) or {}
    node = data.get("name") or data.get("nodeName") or data.get("uuid", "?")

    texts = {
        "node.connection_lost": f"🟡 <b>{node}</b> — панель потеряла связь с нодой",
        "node.connection_restored": f"✅ <b>{node}</b> — связь с нодой восстановлена",
        "node.traffic_notify": f"📊 <b>{node}</b> — достигнут порог трафика",
        "node.disabled": f"⏸ <b>{node}</b> — нода выключена в панели",
        "node.enabled": f"▶️ <b>{node}</b> — нода включена в панели",
    }
    if name in texts:
        # Вебхук приходит напрямую от панели, Alertmanager его не видит —
        # значит и заглушку по ноде здесь нужно проверить самим.
        async with aiohttp.ClientSession() as session:
            if await muted(session, node, "link" if name in LINK_EVENTS else "other"):
                log.info("вебхук %s по ноде %s подавлен заглушкой", name, node)
                return web.Response(text="ok")

        suffix = ""
        if name == "node.connection_lost":
            suffix = "\n<i>жду подтверждения от агента — если сервер жив, придёт уточнение</i>"
        await bot.send_message(CHAT_ID, texts[name] + suffix)

    return web.Response(text="ok")


async def handle_health(request: web.Request) -> web.Response:
    """
    Глубокая проверка: отвечаем "жив" только если хаб реально собирает данные.
    Простое "контейнер запущен" бесполезно — vmagent может молча не скрейпить,
    и мониторинг будет выглядеть работающим, ничего не видя.
    """
    try:
        async with aiohttp.ClientSession() as session:
            alive = await instant(session, 'count(up{job="self"} == 1)')
            fresh = await instant(session, f"count(node:panel_status{_p()})")
    except Exception as exc:
        return web.Response(status=503, text=f"база недоступна: {exc}")

    services = alive[0]["value"] if alive else 0
    nodes = fresh[0]["value"] if fresh else 0

    if services < 3:
        return web.Response(status=503, text=f"живых служб только {services:.0f}")
    if nodes < 1:
        return web.Response(status=503, text="нет свежих метрик от панели")

    return web.Response(text=f"ok: служб {services:.0f}, нод {nodes:.0f}")


async def run_web() -> None:
    app = web.Application()
    app.router.add_post("/alerts", handle_alerts)
    app.router.add_post("/watchdog", handle_watchdog)
    app.router.add_post("/remnawave", handle_remnawave)
    app.router.add_get("/healthz", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8080).start()
    log.info("вебхуки слушают на :8080")


async def main() -> None:
    await run_web()

    # Неверный токен — единственная причина, по которой стартовать бессмысленно.
    # Говорим об этом одной понятной строкой, а не трейсбеком на весь экран.
    try:
        me = await bot.get_me()
        log.info("бот @%s на связи", me.username)
    except TelegramUnauthorizedError:
        log.error("Telegram отверг токен. Проверьте TELEGRAM_BOT_TOKEN в hub/.env "
                  "(после Revoke у BotFather токен меняется) и перезапустите бота.")
        raise SystemExit(1)

    # А вот недоступный чат ронять бота не должен: команды в личке будут
    # работать, и по логу сразу видно, что чинить.
    hello = "🚀 Мониторинг запущен. /status — сводка"
    if PROJECT:
        hello = f"🚀 Мониторинг проекта <b>{PROJECT}</b> запущен. /status — сводка"
    try:
        await bot.send_message(CHAT_ID, hello)
    except Exception as exc:
        log.error("не удалось написать в чат %s: %s. Проверьте TELEGRAM_CHAT_ID "
                  "и что бот добавлен в группу с правом писать.", CHAT_ID, exc)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
