"""
Бот-витрина мониторинга.

Он ничего не собирает и ничего не решает: метрики лежат в VictoriaMetrics,
логику "когда шуметь" держит Alertmanager. Бот только показывает и пересылает.
Поэтому его можно спокойно перезапускать в любой момент — состояния в нём нет.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
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
ALERTMANAGER_URL = os.getenv("ALERTMANAGER_URL", "http://alertmanager:9093")
HEALTHCHECKS_URL = os.getenv("HEALTHCHECKS_URL", "")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ─────────────────────────────────────────────────────────────────────────
# Сбор картины по всем нодам
# ─────────────────────────────────────────────────────────────────────────

# Панель и агент дополняют друг друга: панель отдаёт данные по всем нодам
# сразу, агент — точнее и независимо. Где есть агент, берём его; где нет,
# показываем то, что знает панель. Поэтому у большинства полей два запроса.
QUERIES = {
    "up":        'max by(node) (up{job="node"})',
    "probe_nl":  'max by(node) (probe_success{vantage="nl"})',
    "probe_ru":  'max by(node) (probe_success{job="probe_ru"})',
    "tcp_ru":    'max by(node) (probe_success{job="probe_ru_tcp"})',
    "panel":     "node:panel_status",
    "users":     "node:panel_users",
    # Агент, если раскатан
    "cpu":       "max by(node) (node:cpu_busy:ratio)",
    "mem":       "max by(node) (node:mem_used:ratio)",
    "rx":        "max by(node) (node:net_rx:bps)",
    "tx":        "max by(node) (node:net_tx:bps)",
    # Панель — источник по умолчанию
    "cpu_panel": "node:panel_cpu",
    "mem_panel": "node:panel_mem",
    "rx_panel":  "node:panel_rx",
    "tx_panel":  "node:panel_tx",
}

DETAIL_QUERIES = {
    "steal":     'avg by(node) (rate(node_cpu_seconds_total{mode="steal"}[10m]))',
    "uptime":    "max by(node) (time() - node_boot_time_seconds)",
    "uptime_panel": "node:panel_uptime",
    "p95":       "max by(node) (node:net_tx:p95_24h)",
    "p95_panel": "node:panel_tx_p95_24h",
    "link":      "max by(node) (node:link_capacity:bps)",
    "speedtest": "max by(node) (node_port_speedtest_bps)",
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

    # Флаг страны и провайдер приходят от панели — подписываем ими карточки.
    try:
        for row in await instant(session, "node:name_map"):
            meta = nodes.setdefault(row["node"], {})
            meta["flag"] = row.get("node_country_emoji", "")
            meta["provider"] = row.get("provider_name", "")
    except Exception as exc:
        log.warning("не удалось получить имена нод: %s", exc)

    # Ноды, которые есть в конфиге, но ещё ни разу не отдали метрик,
    # всё равно должны быть видны — иначе пропажу легко не заметить.
    for row in await instant(session, 'up{job="node"}'):
        meta = nodes.setdefault(row.get("node", "?"), {})
        meta.setdefault("hoster", row.get("hoster"))
        meta.setdefault("public_ip", row.get("public_ip"))

    return nodes


# ─────────────────────────────────────────────────────────────────────────
# Команды
# ─────────────────────────────────────────────────────────────────────────

def allowed(message: Message) -> bool:
    return not ADMINS or message.from_user.id in ADMINS


def refresh_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")]]
    )


@dp.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Команды</b>\n"
        "/status — сводка по всем нодам\n"
        "/node &lt;имя&gt; — подробности по ноде\n"
        "/mute &lt;имя&gt; &lt;часы&gt; — заглушить алерты на время работ\n"
        "/unmute &lt;имя&gt; — снять заглушку\n"
    )


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not allowed(message):
        return
    async with aiohttp.ClientSession() as session:
        nodes = await collect(session)
    await message.answer(render.status_table(nodes), reply_markup=refresh_kb())


@dp.callback_query(F.data == "refresh")
async def cb_refresh(call: CallbackQuery) -> None:
    async with aiohttp.ClientSession() as session:
        nodes = await collect(session)
    text = render.status_table(nodes) + f"\n<i>обновлено {time.strftime('%H:%M:%S')}</i>"
    try:
        await call.message.edit_text(text, reply_markup=refresh_kb())
    except Exception:
        pass  # Telegram ругается, если текст не изменился — это нормально
    await call.answer("Обновлено")


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


@dp.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject) -> None:
    """Заглушка на время плановых работ — через Silences в Alertmanager."""
    if not allowed(message):
        return
    parts = (command.args or "").split()
    if not parts:
        await message.answer("Пример: <code>/mute nl-1 2</code> — заглушить на 2 часа")
        return

    name = parts[0]
    hours = float(parts[1]) if len(parts) > 1 else 2.0
    now = time.time()
    payload = {
        "matchers": [{"name": "node", "value": name, "isRegex": False}],
        "startsAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now)),
        "endsAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now + hours * 3600)),
        "createdBy": message.from_user.username or str(message.from_user.id),
        "comment": "плановые работы, заглушено из телеграма",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{ALERTMANAGER_URL}/api/v2/silences", json=payload) as r:
            ok = r.status < 300
            detail = await r.text()
    await message.answer(
        f"🔇 {name} заглушена на {hours:g} ч" if ok else f"Не вышло: {detail}"
    )


@dp.message(Command("unmute"))
async def cmd_unmute(message: Message, command: CommandObject) -> None:
    if not allowed(message):
        return
    name = (command.args or "").strip()
    removed = 0
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{ALERTMANAGER_URL}/api/v2/silences") as r:
            silences = await r.json()
        for s in silences:
            if s.get("status", {}).get("state") != "active":
                continue
            if any(m["name"] == "node" and m["value"] == name for m in s.get("matchers", [])):
                await session.delete(f"{ALERTMANAGER_URL}/api/v2/silences/{s['id']}")
                removed += 1
    await message.answer(f"🔔 {name}: снято заглушек — {removed}")


# ─────────────────────────────────────────────────────────────────────────
# Входящие вебхуки
# ─────────────────────────────────────────────────────────────────────────

async def handle_alerts(request: web.Request) -> web.Response:
    """Алерты от Alertmanager."""
    payload = await request.json()
    for alert in payload.get("alerts", []):
        try:
            await bot.send_message(CHAT_ID, render.alert_message(alert))
        except Exception as exc:
            log.error("не отправился алерт: %s", exc)
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
        suffix = ""
        if name == "node.connection_lost":
            suffix = "\n<i>жду подтверждения от агента — если сервер жив, придёт уточнение</i>"
        await bot.send_message(CHAT_ID, texts[name] + suffix)

    return web.Response(text="ok")


async def run_web() -> None:
    app = web.Application()
    app.router.add_post("/alerts", handle_alerts)
    app.router.add_post("/watchdog", handle_watchdog)
    app.router.add_post("/remnawave", handle_remnawave)
    app.router.add_get("/healthz", lambda r: web.Response(text="ok"))

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
    try:
        await bot.send_message(CHAT_ID, "🚀 Мониторинг запущен. /status — сводка")
    except Exception as exc:
        log.error("не удалось написать в чат %s: %s. Проверьте TELEGRAM_CHAT_ID "
                  "и что бот добавлен в группу с правом писать.", CHAT_ID, exc)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
