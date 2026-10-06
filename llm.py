"""Обёртка над языковой моделью с цепочкой запасных провайдеров.

Основной провайдер задаётся LLM_*, запасные: LLM_FALLBACK_1 ... LLM_FALLBACK_10
в формате  base_url|api_key|model . Если у одного кончился бесплатный лимит, он упал
или (в режиме 18+) модель отказалась отвечать, бот пробует следующий.
Работает с любым OpenAI-совместимым API и с Claude (LLM_PROVIDER=anthropic для основного)."""

import asyncio
import logging
import os
import re
import time

log = logging.getLogger("llm")

PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "")
BASE_URL = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
# с запасом: у некоторых моделей часть лимита уходит на «размышления»
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1000"))

# не больше N запросов к API одновременно, чтобы не упираться в лимиты бесплатного тарифа
_sem = asyncio.Semaphore(int(os.getenv("LLM_CONCURRENCY", "2")))

# если провайдер выдал ошибку (лимит, ключ, модель пропала), на столько секунд он уходит в конец очереди,
# чтобы не тратить время на заведомо мёртвый вариант при каждом сообщении
COOLDOWN_SEC = int(os.getenv("LLM_COOLDOWN", "300"))


def _build_providers() -> list[dict]:
    provs = []
    if API_KEY and MODEL:
        provs.append({"kind": PROVIDER, "key": API_KEY, "model": MODEL, "base": BASE_URL})
    for i in range(1, 11):
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

            p["client"] = AsyncAnthropic(api_key=p["key"], max_retries=0)
        else:
            from openai import AsyncOpenAI

            p["client"] = AsyncOpenAI(api_key=p["key"], base_url=p["base"], max_retries=0)
    return p["client"]


_ACTION = re.compile(r"(^|[.!?…]\s|\n)\*[^*\n]{2,}\*[ \t]*")


_CHECKIN = re.compile(
    r"(?:ты |вы )?не устал[аи]?(?: ли)?\b|не буду настаивать|не стану настаивать|уже устал[аи]?\b|устал[аи]? от (?:игры|меня|этого|разговора|общения)|тебе надоело|"
    r"хочешь (?:остановиться|прекратить|закончить)|если (?:тебе )?(?:неприятно|некомфортно|не хочешь)|"
    r"скажи[,]? (?:стоп|если)|можем остановиться|я не давлю",
    re.I,
)
_SENT = re.compile(r"(?<=[.!?…)])\s+")


_NARR = re.compile(r"^[^—\n]{8,}?[.!?…]\s*—\s+")
_DASH = re.compile(r"^\s*[—–-]\s+")


def _strip_narration(text: str) -> str:
    """Убирает авторскую речь от третьего лица: «Артём наклоняется ближе. — Привет» -> «Привет»."""
    out = []
    for p in re.split(r"\n\s*\n", text):
        q = p.strip()
        m = _NARR.match(q)
        if m:
            q = q[m.end():]
        q = _DASH.sub("", q)
        if q:
            out.append(q)
    return "\n\n".join(out) or text


def clean_reply(text: str) -> str:
    """Чистит ответ модели: действия в звёздочках, пояснения в скобках в конце и дежурные «ты не устал? я не буду настаивать»."""
    t = _strip_narration(text)
    t = _ACTION.sub(r"\1", t)
    t = re.sub(r"\*([^*\n]+)\*", r"\1", t).replace("*", "")
    # последний абзац целиком в скобках: это пояснение от модели, а не реплика
    paras = [p for p in re.split(r"\n\s*\n", t) if p.strip()]
    if len(paras) > 1 and paras[-1].strip().startswith("(") and paras[-1].strip().endswith(")"):
        t = "\n\n".join(paras[:-1])
    # дежурные проверки «не устал ли ты» в хвосте: срезаем с конца, пока есть что оставить
    sents = _SENT.split(t.strip())
    while len(sents) > 1 and _CHECKIN.search(sents[-1]):
        sents.pop()
    t = " ".join(sents) if len(sents) > 1 else (sents[0] if sents else t)
    t = re.sub(r"[ \t]{2,}", " ", t).strip()
    return t or text.replace("*", "").strip()


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
    # Gemini иногда отвечает 200, но без текста (сработал фильтр безопасности или кончились токены на размышления)
    choice = resp.choices[0] if getattr(resp, "choices", None) else None
    msg = getattr(choice, "message", None)
    text = (getattr(msg, "content", None) or "").strip()
    if not text:
        log.info(
            "Модель %s вернула пустой ответ (finish_reason=%s), пробую следующую",
            p["model"], getattr(choice, "finish_reason", None),
        )
    return text


async def generate(system: str, messages: list[dict], nsfw: bool = False) -> str:
    last_exc: Exception | None = None
    refused_reply = ""
    async with _sem:
        now = time.monotonic()
        # сначала те, кто не в паузе, в конце те, кто недавно падал (вдруг уже ожили)
        ready = [p for p in PROVIDERS if p.get("until", 0) <= now]
        resting = [p for p in PROVIDERS if p.get("until", 0) > now]
        for p in ready + resting:
            try:
                reply = await _call(p, system, messages)
            except Exception as e:  # лимит, сеть, модель пропала: идём к следующему
                log.warning("Провайдер %s (%s) не ответил: %s", p["base"], p["model"], str(e)[:300])
                p["until"] = time.monotonic() + COOLDOWN_SEC
                last_exc = e
                continue
            p["until"] = 0
            if not reply:
                continue
            if nsfw and looks_like_refusal(reply):
                log.info("Модель %s отказалась, пробую следующую", p["model"])
                refused_reply = refused_reply or reply
                continue
            return clean_reply(reply)
    if refused_reply:
        return clean_reply(refused_reply)
    if last_exc:
        raise last_exc
    return ""
