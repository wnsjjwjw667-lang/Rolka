"""Веб-версия бота: каталог персонажей и чат в браузере. Запускается вместе с ботом (WEB_ENABLED=0 выключает).

Переменные окружения:
  WEB_ENABLED     1/0  - включить/выключить сайт (по умолчанию 1)
  PORT            порт сайта (по умолчанию 8080)
  WEB_TG_URL      ссылка на Telegram-бота для шапки сайта
  WEB_IP_LIMIT    сообщений в сутки с одного IP (по умолчанию 15)
  WEB_GLOBAL_LIMIT сообщений в сутки со всего сайта (по умолчанию 300)
"""
import logging
import os
import time
from pathlib import Path

from aiohttp import web

import db
import llm
import safety
from characters import BASE_PROMPT

log = logging.getLogger("web")

IP_LIMIT = int(os.getenv("WEB_IP_LIMIT", "15"))          # сообщений в сутки с одного IP
GLOBAL_LIMIT = int(os.getenv("WEB_GLOBAL_LIMIT", "300"))  # сообщений в сутки с сайта всего, чтобы не съесть квоту бота
INDEX = Path(__file__).with_name("static") / "index.html"
_used: dict = {}  # {(день, ip или "*"): сколько}


def _ip(request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return fwd or request.remote or "?"


def _take(ip: str) -> bool:
    d = time.strftime("%Y-%m-%d")
    for k in [k for k in _used if k[0] != d]:
        del _used[k]
    if _used.get((d, "*"), 0) >= GLOBAL_LIMIT or _used.get((d, ip), 0) >= IP_LIMIT:
        return False
    for k in ((d, "*"), (d, ip)):
        _used[k] = _used.get(k, 0) + 1
    return True


def _refund(ip: str) -> None:
    d = time.strftime("%Y-%m-%d")
    for k in ((d, "*"), (d, ip)):
        if _used.get(k, 0) > 0:
            _used[k] -= 1


async def index(request):
    return web.FileResponse(INDEX, headers={"Content-Type": "text/html; charset=utf-8"})


async def characters(request):
    chars = [
        {k: c[k] for k in ("id", "name", "emoji", "tagline", "greeting")}
        for c in db.list_characters()
    ]
    return web.json_response({"characters": chars, "tg": os.getenv("WEB_TG_URL", "")})


async def chat(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad"}, status=400)
    c = db.get_character(data.get("character"))
    raw = data.get("messages") if isinstance(data.get("messages"), list) else []
    msgs = [
        {"role": m["role"], "content": str(m.get("content", ""))[:1000]}
        for m in raw[-20:]
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
    ]
    while msgs and msgs[0]["role"] == "assistant":
        msgs.pop(0)
    if not c or not msgs or msgs[-1]["role"] != "user":
        return web.json_response({"error": "bad"}, status=400)

    text = msgs[-1]["content"]
    if safety.claims_minor(text):
        return web.json_response({"error": "minor"}, status=403)
    if safety.sexual_minor(text):
        return web.json_response({"reply": "С такими темами я не помогаю. Все персонажи только взрослые."})

    ip = _ip(request)
    if not _take(ip):
        return web.json_response({"error": "limit"}, status=429)

    # на сайте всегда обычный режим, без 18+: посетители анонимны
    system = BASE_PROMPT + f"Имя: {c['name']}.\n{c['persona']}" + safety.rules_suffix(False)
    try:
        reply = await llm.generate(system, msgs, nsfw=False)
        if not reply:
            raise ValueError("пустой ответ модели")
    except Exception as e:
        log.warning("Ошибка модели на сайте: %s", str(e)[:200])
        _refund(ip)
        return web.json_response({"error": "busy"}, status=503)
    return web.json_response({"reply": reply[:4000]})


async def start() -> None:
    app = web.Application(client_max_size=64 * 1024)
    app.add_routes([web.get("/", index), web.get("/api/characters", characters), web.post("/api/chat", chat)])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Сайт запущен на порту %d", port)
