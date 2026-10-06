"""Обёртка над языковой моделью с цепочкой запасных провайдеров.

Основной провайдер задаётся LLM_*, запасные: LLM_FALLBACK_1 ... LLM_FALLBACK_5
в формате  base_url|api_key|model . Если у одного кончился бесплатный лимит, он упал
или (в режиме 18+) модель отказалась отвечать, бот пробует следующий.
Работает с любым OpenAI-совместимым API и с Claude (LLM_PROVIDER=anthropic для основного)."""

import asyncio
import logging
import os
import re

log = logging.getLogger("llm")

PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "")
BASE_URL = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
# с запасом: у некоторых моделей часть лимита уходит на «размышления»
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1000"))

# не больше N запросов к API одновременно, чтобы не упираться в лимиты бесплатного тарифа
_sem = asyncio.Semaphore(int(os.getenv("LLM_CONCURRENCY", "4")))


def _build_providers() -> list[dict]:
    provs = []
    if API_KEY and MODEL:
        provs.append({"kind": PROVIDER, "key": API_KEY, "model": MODEL, "base": BASE_URL})
    for i in range(1, 6):
        raw = os.getenv(f"LLM_FALLBACK_{i}", "").strip()
        if not raw:
            continue
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) != 3 or not all(parts):
            log.warning("LLM_FALLBACK_%d пропущен: нужен формат base_url|api_key|model", i)
            continue
        provs.append({"kind": "openai", "base": parts[0], "key": parts[1], "model": parts[2]})
    for p in provs:
        p["client"] = None
    return provs


PROVIDERS = _build_providers()

# Признаки того, что модель отказалась, а не ответила в образе
_REFUSAL = re.compile(
    r"не могу (?:создавать|писать|продолжать|продолжить|помочь с этим|выполнить)"
    r"|как (?:языковая модель|ии\b|искусственный интеллект)"
    r"|i can(?:'|’)?t (?:assist|help|continue|write|create|engage)"
    r"|i cannot (?:assist|help|continue|write|create|engage|fulfill)"
    r"|i(?:'|’)m sorry, but|i am unable to",
    re.I,
)


def looks_like_refusal(reply: str) -> bool:
    return len(reply) < 500 and bool(_REFUSAL.search(reply))


def _client(p: dict):
    if p["client"] is None:
        if p["kind"] == "anthropic":
            from anthropic import AsyncAnthropic

            p["client"] = AsyncAnthropic(api_key=p["key"])
        else:
            from openai import AsyncOpenAI

            p["client"] = AsyncOpenAI(api_key=p["key"], base_url=p["base"])
    return p["client"]


async def _call(p: dict, system: str, messages: list[dict]) -> str:
    client = _client(p)
    if p["kind"] == "anthropic":
        resp = await client.messages.create(
            model=p["model"], max_tokens=MAX_TOKENS, system=system, messages=messages
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    resp = await client.chat.completions.create(
        model=p["model"],
        max_tokens=MAX_TOKENS,
        messages=[{"role": "system", "content": system}, *messages],
    )
    return (resp.choices[0].message.content or "").strip()


async def generate(system: str, messages: list[dict], nsfw: bool = False) -> str:
    last_exc: Exception | None = None
    refused_reply = ""
    async with _sem:
        for p in PROVIDERS:
            try:
                reply = await _call(p, system, messages)
            except Exception as e:  # лимит, сеть, модель пропала: идём к следующему
                log.warning("Провайдер %s (%s) не ответил: %s", p["base"], p["model"], e)
                last_exc = e
                continue
            if not reply:
                continue
            if nsfw and looks_like_refusal(reply):
                log.info("Модель %s отказалась, пробую следующую", p["model"])
                refused_reply = refused_reply or reply
                continue
            return reply
    if refused_reply:
        return refused_reply
    if last_exc:
        raise last_exc
    return ""
