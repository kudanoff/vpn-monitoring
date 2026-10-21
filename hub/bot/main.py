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
# Служебные ноды, которые в панели включены, но в сводке не нужны.
# Регулярное выражение по имени. Выключенные в панели ноды сюда вписывать
# не нужно — их бот узнаёт у самой панели.
_ignore = os.getenv("NODES_IGNORE", r"ipcheker")
# Пустой шаблон совпал бы с любым именем и спрятал все ноды разом.
IGNORE_RE = re.compile(_ignore, re.IGNORECASE) if _ignore else None
# Один хаб обслуживает несколько проектов. Бот показывает только свой:
# у каждого проекта свой бот, свой чат и свои дежурные.
PROJECT = os.getenv("PROJECT", "")
# Откуда брать список выключенных нод: тот же API, что у sync-nodes.sh.
_PU = PROJECT.upper().replace("-", "_")
PANEL_API_URL = os.getenv(f"PANEL_API_URL_{_PU}", "")
PANEL_COOKIE = os.getenv(f"PANEL_COOKIE_{_PU}", "")
# Домен панели для заголовка Host. Нужен, когда в API ходят по приватному
# адресу: Remnawave 3.x смотрит на имя и на признаки обратного прокси, а по
# голому http отвечает «Reverse proxy and HTTPS are required» и рвёт связь.
PANEL_API_HOST = os.getenv(f"PANEL_API_HOST_{_PU}", "")
PANEL_TOKEN_FILE = f"/etc/secrets/panel_api_token_{PROJECT}"
PANEL_POLL_SECONDS = 60
VM_URL = os.getenv("VM_URL", "http://victoriametrics:8428")
# Как часто напоминать об аварии, которая не прошла, в часах. Напоминание
# приходит новым сообщением, а прежнее убирается — в ленте одно на аварию.
REMIND_HOURS = float(os.getenv("REMIND_HOURS", "2"))
# Сколько живёт сообщение о перезагрузке или коротком обрыве, секунд.
# Его достаточно увидеть в уведомлении; в ленте оно не нужно.
EPHEMERAL_SECONDS = int(os.getenv("EPHEMERAL_SECONDS", "15"))
# Обрыв короче этого — перезагрузка или моргание, а не авария. Совпадает
# с выдержкой правил NodeDown и PanelNodeDisconnected (for: 5m) с запасом
# на их вычисление: дольше — о связи уже сообщит авария.
BLIP_SECONDS = 6 * 60
# Как часто бот пересобирает табло, секунд.
BOARD_SECONDS = 60
# Ночью без звука всё, кроме первого сообщения об аварии: отбой, напоминание,
# перезагрузку можно увидеть утром. Часы по Москве, «0-8» — с полуночи
# до восьми. Пусто — без ночного режима.
QUIET_HOURS = os.getenv("QUIET_HOURS", "0-8").strip()
# В Москве нет перехода на летнее время, так что хватает постоянного сдвига
# и не нужна база часовых поясов, которой в slim-образе может не быть.
MSK = datetime.timezone(datetime.timedelta(hours=3))
ALERTMANAGER_URL = os.getenv("ALERTMANAGER_URL", "http://alertmanager:9093")
HEALTHCHECKS_URL = os.getenv("HEALTHCHECKS_URL", "")

# Два вида тишины. Полная — когда нода выведена из строя намеренно и про неё
# не нужно вообще ничего. Лёгкая — когда нода работает, но её связь с панелью
# рвётся: про это уже известно, чинить пока нечем, а всё остальное по ноде
# (лёг сервер, кончается диск) должно доходить как обычно.
LINK_ALERTS = ("NodeFlapping", "PanelNodeDisconnected", "PanelLostNodeButHostAlive")
# По вебхукам панели о связи бот узнаёт перезагрузки: потеря и возврат
# в пределах BLIP_SECONDS — короткое сообщение, которое само исчезает.
LINK_EVENTS = {"node.connection_lost", "node.connection_restored"}
MUTE_KINDS = {
    "full": ("🔇", "молчит всё"),
    "link": ("🔀", "молчат только разрывы связи"),
    # Пауза по кнопке под сообщением — молчит ровно один алерт.
    "pause": ("⏸", "пауза по алерту"),
    # Заглушка, поставленная руками через amtool на какой-то другой алерт.
    "other": ("🔕", "молчит часть алертов"),
}
DEFAULT_HOURS = {"full": 2.0, "link": 24.0 * 7}

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ─────────────────────────────────────────────────────────────────────────
# Выключенные в панели ноды
# ─────────────────────────────────────────────────────────────────────────

# Выключенная в панели нода выведена из работы намеренно: её связь с панелью
# рвётся по определению, и алерты по ней — ложные тревоги. Раньше такие ноды
# узнавали по слову archive в имени; теперь спрашиваем панель напрямую.
# Бот пишет признак в VictoriaMetrics, правило NodeDisabledInPanel поднимает
# по нему тихий алерт, а Alertmanager глушит им все прочие алерты ноды.
_disabled: set[str] = set()


def hidden(node: str) -> bool:
    """Нода, которую не показываем в сводке: выключена в панели или служебная."""
    return node in _disabled or bool(IGNORE_RE and IGNORE_RE.search(node))


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def panel_proxy_headers() -> dict[str, str]:
    """
    Заголовки, без которых панель за обратным прокси не отвечает по приватной
    сети. Домен берём из PANEL_API_HOST_<ПРОЕКТ>, остальное добавляем только
    для http: по https панель и так считает запрос пришедшим как надо.
    """
    headers: dict[str, str] = {}
    if PANEL_API_HOST:
        headers["Host"] = PANEL_API_HOST
    if PANEL_API_URL.startswith("http://"):
        headers["X-Forwarded-Proto"] = "https"
        headers["X-Forwarded-For"] = os.getenv("HUB_PRIVATE_IP", "10.77.0.1")
    return headers


async def fetch_panel_nodes(session: aiohttp.ClientSession) -> list[dict]:
    with open(PANEL_TOKEN_FILE) as f:
        token = f.read().strip()
    headers = {"Authorization": f"Bearer {token}"}
    if PANEL_COOKIE:
        headers["Cookie"] = PANEL_COOKIE
    headers.update(panel_proxy_headers())
    async with session.get(f"{PANEL_API_URL.rstrip('/')}/api/nodes", headers=headers,
                           timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status != 200:
            raise RuntimeError(f"API панели ответил кодом {r.status}")
        body = await r.json(content_type=None)
    # Разные версии панели заворачивают список по-разному — как в sync-nodes.sh.
    resp = body.get("response", body) if isinstance(body, dict) else body
    if isinstance(resp, dict):
        resp = resp.get("nodes")
    return resp if isinstance(resp, list) else []


async def push_disabled(session: aiohttp.ClientSession, names: list[str]) -> None:
    """
    Признак пишется по каждой ноде, нулём или единицей. Включённую ноду
    нужно явно обнулить: иначе единица провисит в базе ещё пять минут
    и всё это время будет глушить настоящие алерты.
    """
    lines = [
        f'vpnmon_node_disabled{{project="{_label(PROJECT)}",node="{_label(n)}"}} '
        f"{1 if n in _disabled else 0}"
        for n in names
    ]
    if not lines:
        return
    async with session.post(f"{VM_URL}/api/v1/import/prometheus", data="\n".join(lines) + "\n",
                            timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status >= 300:
            raise RuntimeError(f"VictoriaMetrics ответила {r.status}: {await r.text()}")


async def watch_panel() -> None:
    """
    Раз в минуту сверяемся с панелью. Если панель недоступна, помним прошлый
    список, но в базу его не пишем: признак через несколько минут устареет,
    и алерты по нодам снова пойдут. Лишний шум лучше молчания вслепую.
    """
    global _disabled
    if not (PROJECT and PANEL_API_URL):
        log.warning("PANEL_API_URL_%s не задан — выключенные в панели ноды не отслеживаются", _PU)
        return
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                nodes = await fetch_panel_nodes(session)
                if nodes:
                    now = {n["name"] for n in nodes if n.get("name") and n.get("isDisabled")}
                    if now != _disabled:
                        log.info("выключены в панели: %s", ", ".join(sorted(now)) or "нет")
                    _disabled = now
                    await push_disabled(session, [n["name"] for n in nodes if n.get("name")])
                else:
                    log.warning("API панели вернул пустой список нод")
            except Exception as exc:
                log.warning("не удалось узнать у панели выключенные ноды: %s", exc)
            await asyncio.sleep(PANEL_POLL_SECONDS)


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

    # Отсекаем выключенные в панели и служебные ноды: они всегда "отключены"
    # и, если их не убрать, сводка на две трети состоит из ложных тревог.
    for name in [n for n in nodes if hidden(n)]:
        del nodes[name]

    # Флаг страны и провайдер приходят от панели — подписываем ими карточки.
    try:
        for row in await instant(session, f"node:name_map{_p()}"):
            if row["node"] not in nodes:
                continue
            meta = nodes[row["node"]]
            meta["flag"] = row.get("node_country_emoji", "")
            meta["provider"] = row.get("provider_name", "")
    except Exception as exc:
        log.warning("не удалось получить имена нод: %s", exc)

    # Ноды, которые есть в конфиге, но ещё ни разу не отдали метрик,
    # всё равно должны быть видны — иначе пропажу легко не заметить.
    for row in await instant(session, f'up{_p("job=~\"node.*\"")}'):
        if hidden(row.get("node", "")):
            continue
        meta = nodes.setdefault(row.get("node", "?"), {})
        meta.setdefault("hoster", row.get("hoster"))
        meta.setdefault("public_ip", row.get("public_ip"))

    return nodes


# ─────────────────────────────────────────────────────────────────────────
# Команды
# ─────────────────────────────────────────────────────────────────────────

def allowed(message: Message) -> bool:
    return allowed_user(message.from_user.id)


def allowed_user(user_id: int) -> bool:
    return not ADMINS or user_id in ADMINS


def _author(user) -> str:
    return user.username or str(user.id)


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
        "<i>Под каждым сообщением об аварии есть кнопки паузы: 🔇 2 ч., 8 ч., "
        "24 ч., 2 дн. Пауза останавливает напоминания только об этой проблеме; "
        "кнопка «Продолжить уведомлять» снимает её и начинает цикл заново.</i>\n\n"
        "<i>Срок: 30м, 2ч, 7д, 2н; по умолчанию 2 ч и 7 д соответственно.\n"
        "Имя с пробелами — в кавычках: /mute \"DE Frankfurt 1\" 3ч</i>\n\n"
        "<i>Выключенные в панели ноды скрыты, алерты по ним не приходят. "
        "Служебные ноды прячет переменная NODES_IGNORE в .env</i>"
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


def _eq(name: str, value: str, regex: bool = False) -> dict:
    return {"name": name, "value": value, "isRegex": regex, "isEqual": True}


def _matchers(node: str | None, kind: str, alertname: str | None = None) -> list[dict]:
    m = [_eq("node", node)] if node else []
    # Alertmanager один на все проекты, а имена нод в разных панелях
    # совпадают. Без этого матчера заглушка в одном проекте затыкает
    # одноимённую ноду в соседнем.
    if PROJECT:
        m.append(_eq("project", PROJECT))
    if alertname:
        m.append(_eq("alertname", alertname))          # пауза по кнопке — один алерт
    elif kind == "link":
        m.append(_eq("alertname", "|".join(LINK_ALERTS), regex=True))
    return m


async def add_silence(session: aiohttp.ClientSession, node: str | None, hours: float,
                      kind: str, author: str, alertname: str | None = None,
                      comment: str = "") -> str | None:
    """Ставит заглушку и возвращает её id. None — если Alertmanager отказал."""
    now = time.time()
    payload = {
        "matchers": _matchers(node, kind, alertname),
        "startsAt": _fmt_ts(now),
        "endsAt": _fmt_ts(now + hours * 3600),
        "createdBy": author,
        "comment": comment or f"mute:{kind} — заглушено из телеграма",
    }
    async with session.post(f"{ALERTMANAGER_URL}/api/v2/silences", json=payload) as r:
        body = await r.text()
        if r.status >= 300:
            log.warning("Alertmanager не принял заглушку: %s", body)
            return None
    _mute_cache["at"] = 0.0  # чтобы вебхуки панели замолчали сразу
    try:
        return json.loads(body).get("silenceID") or None
    except ValueError:
        return None


async def fetch_mutes(session: aiohttp.ClientSession) -> list[dict]:
    """Действующие заглушки по нодам нашего проекта."""
    async with session.get(f"{ALERTMANAGER_URL}/api/v2/silences") as r:
        items = await r.json()
    return [s for s in items
            if s.get("status", {}).get("state") == "active" and mute_label(s)]


def mute_node(silence: dict) -> str | None:
    """Имя ноды из заглушки — или None, если заглушка не про ноду и не наша."""
    node = None
    for m in silence.get("matchers", []):
        if m.get("name") == "project" and PROJECT and m.get("value") != PROJECT:
            return None
        if m.get("name") == "node" and not m.get("isRegex"):
            node = m.get("value")
    return node


def mute_label(silence: dict) -> str | None:
    """Чем подписать заглушку в списке: нодой, а если её нет — именем алерта."""
    if mute_node(silence):
        return mute_node(silence)
    if PROJECT and not any(m.get("name") == "project" and m.get("value") == PROJECT
                           for m in silence.get("matchers", [])):
        return None
    for m in silence.get("matchers", []):
        if m.get("name") == "alertname":
            return m.get("value")
    return None


def mute_kind(silence: dict) -> str:
    """
    Заглушка без матчера по alertname глушит по ноде всё — это full.
    Если матчер есть и в нём наши «связные» алерты — это link. Прочие
    ручные заглушки из amtool попадают в other и вебхуков не касаются.
    """
    for m in silence.get("matchers", []):
        if m.get("name") != "alertname":
            continue
        if not m.get("isRegex"):
            return "pause"                      # пауза по одному алерту, кнопкой
        return "link" if any(a in (m.get("value") or "") for a in LINK_ALERTS) else "other"
    return "full"


def mute_alertname(silence: dict) -> str:
    """Имя алерта у паузы — чтобы в списке было видно, что именно молчит."""
    for m in silence.get("matchers", []):
        if m.get("name") == "alertname" and not m.get("isRegex"):
            return m.get("value") or ""
    return ""


def mute_covers(silence: dict, kind: str) -> bool:
    """Накрывает ли заглушка событие этого рода (для вебхуков панели)."""
    k = mute_kind(silence)
    if k == "full":
        return True
    if kind != "link":
        return False
    return k == "link" or (k == "pause" and mute_alertname(silence) in LINK_ALERTS)


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
    return any(mute_node(s) == node and mute_covers(s, kind) for s in _mute_cache["items"])


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
        names |= {r["node"] for r in rows if r.get("node") and not hidden(r["node"])}
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
        sid = await add_silence(session, node, hours, kind, _author(message.from_user))

    if not sid:
        await message.answer("Не вышло заглушить: Alertmanager отказал, подробности в логе бота")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔔 Вернуть звук", callback_data=f"unmute:{sid}")
    ]])
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
        "node": mute_label(s),
        "kind": mute_kind(s),
        "alert": mute_alertname(s),
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
                     silence_ids: list[str] | None = None) -> int:
    removed = 0
    for s in await fetch_mutes(session):
        if silence_ids is not None and s.get("id") not in silence_ids:
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
        removed = await drop_mutes(session, silence_ids=[call.data.split(":", 1)[1]])
    await call.answer("Звук вернулся" if removed else "Заглушка уже снята")
    try:
        await call.message.edit_text("🔔 Заглушка снята", reply_markup=None)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────
# Кнопки паузы под сообщением об аварии
# ─────────────────────────────────────────────────────────────────────────

# Самый частый случай: проблема видна, руки до неё дойдут не сейчас, а
# напоминания каждые несколько минут мешают работать. Кнопка ставит такую же
# заглушку, как /mute, но ровно по этому алерту и этим нодам — всё остальное
# по ним продолжает приходить.
SNOOZE_STEPS = [(2, "🔇 2 ч."), (8, "🔇 8 ч."), (24, "🔇 24 ч."), (48, "🔇 2 дн.")]

# Что именно глушит кнопка. В callback_data помещается 64 байта, а имена нод
# бывают длинные, поэтому там едет только короткий отпечаток группы алертов.
# Отпечаток считается от groupKey: после перезапуска бота память пуста, но
# первое же новое сообщение по той же аварии оживляет кнопки старого.
_snoozes: dict[str, dict] = {}


def snooze_token(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def snooze_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=title, callback_data=f"zz:{token}:{hours}")
        for hours, title in SNOOZE_STEPS
    ]])


def resume_button(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔔 Продолжить уведомлять", callback_data=f"zz-on:{token}")
    ]])


def remember_group(payload: dict, key: str) -> str:
    """Запоминает, к чему относится сообщение, и возвращает его отпечаток."""
    alerts = payload.get("alerts", [])
    labels = payload.get("groupLabels", {})
    alertname = labels.get("alertname") or (alerts[0].get("labels", {}).get("alertname") if alerts else "")
    token = snooze_token(key)
    _snoozes[token] = {
        "key": key,
        "alertname": alertname,
        "nodes": sorted({a.get("labels", {}).get("node") for a in alerts
                         if a.get("labels", {}).get("node")}),
    }
    return token


async def cb_context(call: CallbackQuery, prefix: str) -> dict | None:
    if not allowed_user(call.from_user.id):
        await call.answer("Нет прав", show_alert=True)
        return None
    ctx = _snoozes.get(call.data[len(prefix):].split(":")[0])
    if ctx is None:
        # Бота перезапустили, а сообщение осталось. Врать кнопкой нельзя:
        # человек решит, что пауза поставлена, и пропустит аварию.
        await call.answer("Кнопка от старого сообщения: бота перезапускали, и чему она "
                          "соответствует, он уже не помнит. Поставить паузу — /mute, "
                          "снять — /unmute, посмотреть — /mutes", show_alert=True)
    return ctx


@dp.callback_query(F.data.startswith("zz:"))
async def cb_snooze(call: CallbackQuery) -> None:
    ctx = await cb_context(call, "zz:")
    if ctx is None:
        return
    hours = float(call.data.rsplit(":", 1)[1])
    token = snooze_token(ctx["key"])

    async with aiohttp.ClientSession() as session:
        targets = ctx["nodes"] or [None]   # алерт без ноды глушим по имени
        ids = [await add_silence(session, node, hours, "snooze", _author(call.from_user),
                                 alertname=ctx["alertname"],
                                 comment="пауза по кнопке под сообщением")
               for node in targets]
    ids = [i for i in ids if i]
    if not ids:
        await call.answer("Alertmanager не принял паузу, подробности в логе", show_alert=True)
        return

    ctx["silences"] = ids
    ctx["text"] = call.message.html_text
    # Когда пауза кончится, сообщение об аварии придёт заново, а это,
    # «на паузе», будет убрано.
    restart_incident(ctx["key"])

    await call.answer(f"Пауза на {render.duration(hours * 3600)}")
    try:
        await call.message.edit_text(
            f"{ctx['text']}\n\n⏸ <i>на паузе {render.duration(hours * 3600)}</i>",
            reply_markup=resume_button(token),
        )
    except Exception as exc:
        log.warning("не вышло обновить сообщение с паузой: %s", exc)


@dp.callback_query(F.data.startswith("zz-on:"))
async def cb_resume(call: CallbackQuery) -> None:
    ctx = await cb_context(call, "zz-on:")
    if ctx is None:
        return
    token = snooze_token(ctx["key"])

    async with aiohttp.ClientSession() as session:
        await drop_mutes(session, silence_ids=ctx.pop("silences", []))
    restart_incident(ctx["key"])

    await call.answer("Уведомления возобновлены")
    try:
        await call.message.edit_text(ctx.get("text") or call.message.html_text,
                                     reply_markup=snooze_buttons(token))
    except Exception as exc:
        log.warning("не вышло вернуть кнопки: %s", exc)


# ─────────────────────────────────────────────────────────────────────────
# Входящие вебхуки
# ─────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────
#
# Как бот шумит. В ленту попадают только аварии — то, из-за чего клиенты
# не могут подключиться: сервер лёг, нода заблокирована из РФ, закрыт порт
# xray (severity: critical). Всё прочее — нагрузка, диск, флапы, частые
# перезагрузки — видно на табло, закреплённом сообщении, которое бот
# правит раз в минуту. Перезагрузка, после которой нода сама поднялась,
# — короткое сообщение, которое через EPHEMERAL_SECONDS исчезает.
#
# Авария живёт в ленте одним сообщением: напоминание раз в REMIND_HOURS
# приходит новым, а старое убирается; когда нода вернулась, приходит
# «снова в строю», а сообщение об аварии убирается.
#
# ─────────────────────────────────────────────────────────────────────────

# Аварии по группам алертов Alertmanager. В памяти: после перезапуска бота
# сообщение придёт заново, и это правильнее, чем молчать из-за потерянного
# состояния.
_incidents: dict[str, dict] = {}
# Нода → до какого момента о ней не писать коротких сообщений. Перезагрузку
# видят и вебхук панели, и правило NodeRebooted — сказать о ней надо один раз,
# а после аварии и подавно не нужно: «снова в строю» уже всё сказал.
_quiet_until: dict[str, float] = {}
# Нода → когда панель сообщила о потере связи.
_lost_at: dict[str, float] = {}
# Ссылки на фоновые задачи, иначе сборщик мусора может прибить их на ходу.
_tasks: set[asyncio.Task] = set()


def night() -> bool:
    if not QUIET_HOURS:
        return False
    try:
        start, end = (int(x) for x in QUIET_HOURS.split("-"))
    except ValueError:
        return False
    hour = datetime.datetime.now(MSK).hour
    return start <= hour < end if start <= end else hour >= start or hour < end


async def _send(text: str, keyboard: InlineKeyboardMarkup | None = None,
                silent: bool = False) -> int | None:
    """Отправляет сообщение в чат и возвращает его id — чтобы потом убрать или поправить."""
    try:
        sent = await bot.send_message(CHAT_ID, text, reply_markup=keyboard, disable_notification=silent)
    except Exception as exc:
        log.error("не отправилось сообщение: %s", exc)
        return None
    return sent.message_id


async def _edit(msg: int | None, text: str, keyboard: InlineKeyboardMarkup | None = None) -> bool:
    if not msg:
        return False
    try:
        await bot.edit_message_text(text, chat_id=CHAT_ID, message_id=msg, reply_markup=keyboard)
        return True
    except Exception as exc:
        # Текст не изменился — правка не нужна, сообщение на месте.
        if "not modified" in str(exc):
            return True
        log.warning("не вышло поправить сообщение %s: %s", msg, exc)
        return False


async def _drop(msg: int | None) -> None:
    """Убирает своё сообщение. Не вышло (старше 48 ч) — не беда, останется."""
    if not msg:
        return
    try:
        await bot.delete_message(CHAT_ID, msg)
    except Exception as exc:
        log.warning("не вышло убрать сообщение %s: %s", msg, exc)


def _later(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def ephemeral(node: str, text: str) -> None:
    """Сообщение, которое достаточно увидеть в уведомлении: через несколько секунд оно исчезает."""
    now = time.time()
    if _quiet_until.get(node, 0) > now or in_incident(node):
        return
    _quiet_until[node] = now + 15 * 60
    msg = await _send(text, silent=night())

    async def drop_later() -> None:
        await asyncio.sleep(EPHEMERAL_SECONDS)
        await _drop(msg)
    if msg:
        _later(drop_later())


def in_incident(node: str) -> bool:
    return any(a.get("labels", {}).get("node") == node
               for st in _incidents.values() for a in st["alerts"].values())


def restart_incident(key: str) -> None:
    """Следующий вызов Alertmanager пришлёт сообщение об аварии заново и уберёт это."""
    state = _incidents.get(key)
    if state:
        state["repost"] = True


def _fp(alert: dict) -> str:
    return alert.get("fingerprint", "")


async def handle_alerts(request: web.Request) -> web.Response:
    """
    Алерты от Alertmanager. Он стучится ежеминутно, а решение «писать или
    промолчать» принимается здесь. В ленту идут только аварии (critical)
    и разовые сообщения о перезагрузке (notify: ephemeral); остальное
    показывает табло, и сюда за ним ходить не нужно.
    """
    payload = await request.json()
    alerts = payload.get("alerts", [])
    firing = [a for a in alerts if a.get("status") == "firing"]
    resolved = [a for a in alerts if a.get("status") == "resolved"]
    key = payload.get("groupKey") or str(sorted(payload.get("groupLabels", {}).items()))
    labels = payload.get("commonLabels") or (alerts[0].get("labels", {}) if alerts else {})
    now = time.time()

    if labels.get("notify") == "ephemeral":
        for a in firing:
            node = a.get("labels", {}).get("node", "")
            await ephemeral(node, f"🔄 <b>{node}</b> — перезагрузилась и сама поднялась")
        return web.Response(text="ok")

    if labels.get("severity") != "critical":
        return web.Response(text="ok")  # это для табло

    state = _incidents.get(key)
    known = state["alerts"] if state else {}
    back = [a for a in resolved if _fp(a) in known]
    fresh = [a for a in firing if _fp(a) not in known]

    if back:
        # Ноды вернулись — об этом стоит знать, но будить ночью незачем.
        for a in back:
            _quiet_until[a.get("labels", {}).get("node", "")] = now + 15 * 60
        await _send(render.recovered(back), silent=night())

    if not firing:
        _incidents.pop(key, None)
        _snoozes.pop(snooze_token(key), None)
        if state:
            await _drop(state["msg"])
        return web.Response(text="ok")

    def post(still: bool = False):
        group = {**payload, "alerts": firing}
        return (render.incident(firing, now, still=still),
                snooze_buttons(remember_group(group, key)))

    if state is None or fresh or state.get("repost"):
        # Новая авария или к ней добавились ноды: одно сообщение со всеми
        # нодами сразу, со звуком в любое время. Прежнее убирается.
        msg = await _send(*post(), silent=False)
        if state and msg:
            await _drop(state["msg"])
        _incidents[key] = {"alerts": {_fp(a): a for a in firing}, "last": now,
                           "msg": msg or (state and state["msg"])}
        return web.Response(text="ok")

    if set(_fp(a) for a in firing) != set(known):
        # Часть нод вернулась (или её заглушили) — убираем их из списка, тихо.
        state["alerts"] = {_fp(a): a for a in firing}
        await _edit(state["msg"], *post())
        return web.Response(text="ok")

    if now - state["last"] >= REMIND_HOURS * 3600:
        msg = await _send(*post(still=True), silent=night())
        if msg:
            await _drop(state["msg"])
            state["msg"] = msg
        state["last"] = now
    return web.Response(text="ok")


# ── табло ────────────────────────────────────────────────────────────────

BOARD_MARK = "📌"


async def board_alerts(session: aiohttp.ClientSession) -> list[dict]:
    """Что горит сейчас: не заглушено, не подавлено, по нашему проекту."""
    params = {"active": "true", "silenced": "false", "inhibited": "false"}
    async with session.get(f"{ALERTMANAGER_URL}/api/v2/alerts", params=params,
                           timeout=aiohttp.ClientTimeout(total=15)) as r:
        items = await r.json()
    out = []
    for a in items:
        lb = a.get("labels", {})
        # Алерт без проекта — общий для хаба (панель не отдаёт метрики):
        # его показываем всем.
        if PROJECT and lb.get("project", PROJECT) != PROJECT:
            continue
        if lb.get("severity") in ("silent", "watchdog") or lb.get("notify") == "ephemeral":
            continue
        if lb.get("node") and hidden(lb["node"]):
            continue
        out.append(a)
    return out


async def find_board() -> int | None:
    """
    Табло — закреплённое сообщение бота. После перезапуска находим его
    по закрепу, а не храним id на диске: перезапуск остаётся безопасным.
    """
    try:
        me = await bot.get_me()
        chat = await bot.get_chat(CHAT_ID)
    except Exception as exc:
        log.warning("не вышло узнать закреп чата: %s", exc)
        return None
    pinned = chat.pinned_message
    if pinned and pinned.from_user and pinned.from_user.id == me.id \
            and (pinned.text or "").startswith(BOARD_MARK):
        return pinned.message_id
    return None


async def new_board(text: str) -> int | None:
    msg = await _send(text, silent=True)
    if msg:
        try:
            await bot.pin_chat_message(CHAT_ID, msg, disable_notification=True)
        except Exception as exc:
            log.warning("табло не закрепилось — дайте боту право закреплять сообщения: %s", exc)
    return msg


async def watch_board() -> None:
    """Раз в минуту пересобирает табло и правит его, если что-то изменилось."""
    msg = await find_board()
    last = ""
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                alerts = await board_alerts(session)
                total = len(await known_nodes(session))
                now = time.time()
                stamp = datetime.datetime.now(MSK).strftime("%H:%M")
                text = render.board(alerts, total, now, stamp)
                # Время обновления меняется каждую минуту, сравниваем без него.
                body = text.rsplit("\n", 1)[0]
                if body != last or not msg:
                    if not await _edit(msg, text):
                        msg = await new_board(text)
                    last = body
            except Exception as exc:
                log.warning("табло не обновилось: %s", exc)
            await asyncio.sleep(BOARD_SECONDS)


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

    # Отмечаем сразу, не дожидаясь опроса: алерты по ноде должны замолчать
    # ещё до того, как панель заметит потерю связи. В чат об этом не пишем:
    # ноду выключил человек, ему и так известно.
    if name == "node.disabled":
        _disabled.add(node)
        return web.Response(text="ok")
    if name == "node.enabled":
        _disabled.discard(node)
        return web.Response(text="ok")
    if node in _disabled or hidden(node):
        return web.Response(text="ok")

    if name == "node.connection_lost":
        _lost_at[node] = time.time()
        return web.Response(text="ok")

    if name == "node.connection_restored":
        lost = _lost_at.pop(node, None)
        gone = time.time() - lost if lost else None
        # Дольше BLIP_SECONDS — это уже была авария, о ней скажет «снова в строю».
        if gone is None or gone > BLIP_SECONDS:
            return web.Response(text="ok")
        # Вебхук приходит напрямую от панели, Alertmanager его не видит —
        # значит и заглушку по ноде здесь нужно проверить самим.
        async with aiohttp.ClientSession() as session:
            if await muted(session, node, "link"):
                return web.Response(text="ok")
        await ephemeral(node, f"🔄 <b>{node}</b> — связи не было {render.duration(gone)}, "
                              f"сама вернулась (перезагрузка?)")
        return web.Response(text="ok")

    if name == "node.traffic_notify":
        async with aiohttp.ClientSession() as session:
            if await muted(session, node, "other"):
                return web.Response(text="ok")
        await _send(f"📊 <b>{node}</b> — достигнут порог трафика", silent=True)

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
    # Ссылку держим, иначе сборщик мусора может прибить задачу на ходу.
    watcher = asyncio.create_task(watch_panel())  # noqa: F841

    # Неверный токен — единственная причина, по которой стартовать бессмысленно.
    # Говорим об этом одной понятной строкой, а не трейсбеком на весь экран.
    try:
        me = await bot.get_me()
        log.info("бот @%s на связи", me.username)
    except TelegramUnauthorizedError:
        log.error("Telegram отверг токен. Проверьте TELEGRAM_BOT_TOKEN в hub/.env "
                  "(после Revoke у BotFather токен меняется) и перезапустите бота.")
        raise SystemExit(1)

    # Приветствия в чат больше нет: что бот жив, видно по времени на табло.
    # Недоступный чат ронять бота не должен — табло напишет об этом в лог.
    board = asyncio.create_task(watch_board())  # noqa: F841

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
