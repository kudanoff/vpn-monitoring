"""Тонкая обёртка над HTTP API VictoriaMetrics (диалект PromQL)."""
import os
import aiohttp

VM_URL = os.getenv("VM_URL", "http://victoriametrics:8428")


async def instant(session: aiohttp.ClientSession, expr: str) -> list[dict]:
    """Мгновенный запрос. Возвращает список {labels..., value}."""
    async with session.get(
        f"{VM_URL}/api/v1/query", params={"query": expr}, timeout=aiohttp.ClientTimeout(total=15)
    ) as r:
        body = await r.json()
    if body.get("status") != "success":
        raise RuntimeError(body.get("error", "запрос не выполнен"))
    out = []
    for item in body["data"]["result"]:
        row = dict(item["metric"])
        row["value"] = float(item["value"][1])
        out.append(row)
    return out


async def by_node(session: aiohttp.ClientSession, expr: str, label: str = "node") -> dict[str, float]:
    """То же самое, но сразу свёрнутое в словарь {имя ноды: значение}."""
    try:
        return {row[label]: row["value"] for row in await instant(session, expr) if label in row}
    except Exception:
        return {}
