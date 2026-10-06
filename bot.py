import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.keyboard import InlineKeyboardBuilder

import admin
import db
import llm
import safety
from characters import BASE_PROMPT
from config import ADMIN_IDS, BOT_TOKEN, CHANNEL_URL, HISTORY_LIMIT, REQUIRED_CHANNEL

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

router = Router()
router.message.filter(F.chat.type == "private")


# ---------- вспомогательное ----------

def characters_keyboard():
    kb = InlineKeyboardBuilder()
    for c in db.list_characters():
        kb.button(text=f"{c['emoji']} {c['name']}", callback_data=f"char:{c['id']}")
    kb.adjust(2)
    return kb.as_markup()


def characters_text() -> str:
    chars = db.list_characters()
    if not chars:
        return "Персонажей пока нет, загляни позже."
    lines = ["Выбери, с кем хочешь поболтать:\n"]
    for c in chars:
        lines.append(f"{c['emoji']} <b>{c['name']}</b> — {c['tagline']}")
    return "\n".join(lines)


MODE_TEXT = (
    "🔞 <b>Режим 18+</b>\n\n"
    "Включить откровенное общение? Персонажи смогут флиртовать открыто и вести откровенные сцены, "
    "если ты сам этого хочешь. Нажимая «Включить», ты подтверждаешь, что тебе есть 18 лет и ты хочешь такой контент."
)


def mode_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔞 Включить", callback_data="nsfw:on")
    kb.button(text="Не надо", callback_data="nsfw:off")
    kb.adjust(2)
    return kb.as_markup()


async def show_menu(message: Message):
    await message.answer(characters_text(), reply_markup=characters_keyboard(), parse_mode="HTML")


async def is_subscribed(bot: Bot, uid: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, uid)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning("Не удалось проверить подписку (бот должен быть админом канала): %s", e)
        return True


def subscribe_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Подписаться на канал", url=CHANNEL_URL)
    kb.button(text="✅ Я подписался", callback_data="check_sub")
    kb.adjust(1)
    return kb.as_markup()


async def notify_admins(bot: Bot, text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception:
            pass


def who(user) -> str:
    return f"{user.id}" + (f" (@{user.username})" if user.username else "")


# ---------- команды ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    u = db.touch_user(message.from_user.id, message.from_user.username)
    if u["banned"]:
        await message.answer("Доступ к боту ограничен.")
        return
    if not u["adult"]:
        kb = InlineKeyboardBuilder()
        kb.button(text="Мне есть 18 лет", callback_data="adult:yes")
        await message.answer(
            "Привет! 👋 Это чат с вымышленными ИИ-персонажами. Бот только для совершеннолетних.\n\n"
            "Нажимая кнопку ниже, ты подтверждаешь, что тебе есть 18 лет.",
            reply_markup=kb.as_markup(),
        )
        return
    await show_menu(message)


@router.message(Command("characters"))
async def cmd_characters(message: Message):
    u = db.touch_user(message.from_user.id, message.from_user.username)
    if u["banned"]:
        await message.answer("Доступ к боту ограничен.")
        return
    if not u["adult"]:
        await message.answer("Сначала нажми /start и подтверди возраст.")
        return
    await show_menu(message)


@router.message(Command("mode"))
async def cmd_mode(message: Message):
    u = db.touch_user(message.from_user.id, message.from_user.username)
    if u["banned"]:
        await message.answer("Доступ к боту ограничен.")
        return
    if not u["adult"]:
        await message.answer("Сначала нажми /start и подтверди возраст.")
        return
    if not db.nsfw_allowed_globally():
        await message.answer("Откровенный режим сейчас отключён администратором.")
        return
    if u["nsfw"]:
        kb = InlineKeyboardBuilder()
        kb.button(text="Выключить 🔞", callback_data="nsfw:off")
        await message.answer("Сейчас откровенный режим включён.", reply_markup=kb.as_markup())
    else:
        await message.answer(MODE_TEXT, reply_markup=mode_keyboard(), parse_mode="HTML")


@router.message(Command("nsfw_all"))
async def cmd_nsfw_all(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    arg = (message.text or "").split(maxsplit=1)[1:] or [""]
    if arg[0].strip().lower() not in ("on", "off"):
        state = "включён" if db.nsfw_allowed_globally() else "выключен"
        await message.answer(f"Общий режим 18+ сейчас {state}. Команда: /nsfw_all on или /nsfw_all off")
        return
    db.set_setting("nsfw_allowed", "1" if arg[0].strip().lower() == "on" else "0")
    await message.answer("Готово. Режим 18+ для всех: " + arg[0].strip().lower())


@router.message(Command("reset"))
async def cmd_reset(message: Message):
    db.clear_history(message.from_user.id)
    await message.answer("Память диалога очищена. Можем начать заново 🙂")


@router.message(Command("help"))
async def cmd_help(message: Message):
    n = db.daily_limit()
    limit = "без лимита" if n <= 0 else f"{n} сообщений в день"
    text = (
        "/characters — выбрать персонажа\n"
        "/mode — режим 18+ (вкл/выкл)\n"
        "/reset — очистить память диалога\n"
        f"Лимит: {limit}.\n\n"
        "Все персонажи вымышленные, отвечает ИИ."
    )
    if message.from_user.id in ADMIN_IDS:
        text += "\n\n/admin — админка"
    await message.answer(text)


# ---------- кнопки ----------

@router.callback_query(F.data == "adult:yes")
async def cb_adult(call: CallbackQuery):
    u = db.touch_user(call.from_user.id, call.from_user.username)
    if u["banned"]:
        await call.answer("Доступ ограничен", show_alert=True)
        return
    db.set_adult(call.from_user.id)
    await call.answer()
    await call.message.edit_text("Отлично, спасибо!")
    if db.nsfw_allowed_globally():
        await call.message.answer(MODE_TEXT, reply_markup=mode_keyboard(), parse_mode="HTML")
    else:
        await show_menu(call.message)


@router.callback_query(F.data.startswith("nsfw:"))
async def cb_nsfw(call: CallbackQuery):
    uid = call.from_user.id
    u = db.touch_user(uid, call.from_user.username)
    if u["banned"] or not u["adult"]:
        await call.answer("Сначала подтверди возраст через /start", show_alert=True)
        return
    on = call.data.endswith(":on") and db.nsfw_allowed_globally()
    db.set_nsfw(uid, on)
    await call.answer()
    await call.message.edit_text(
        "🔞 Откровенный режим включён. Выключить можно в любой момент командой /mode."
        if on else "Откровенный режим выключен. Включить можно командой /mode."
    )
    if (call.message.text or "").startswith("🔞 Режим 18+"):  # это был шаг при первом входе
        await show_menu(call.message)


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, call.from_user.id):
        await call.answer("Спасибо! Теперь можно писать 🙂", show_alert=True)
    else:
        await call.answer("Подписка пока не найдена", show_alert=True)


@router.callback_query(F.data.startswith("char:"))
async def cb_character(call: CallbackQuery):
    uid = call.from_user.id
    u = db.touch_user(uid, call.from_user.username)
    if u["banned"]:
        await call.answer("Доступ ограничен", show_alert=True)
        return
    if not u["adult"]:
        await call.answer("Сначала подтверди возраст через /start", show_alert=True)
        return
    c = db.get_character(call.data.split(":", 1)[1])
    if not c:
        await call.answer("Персонаж не найден", show_alert=True)
        return
    db.set_character(uid, c["id"])
    db.add_message(uid, "user", "(начало разговора)")
    db.add_message(uid, "assistant", c["greeting"])
    await call.answer()
    await call.message.answer(f"{c['emoji']} <b>{c['name']}</b>\n\n{c['greeting']}", parse_mode="HTML")


# ---------- диалог ----------

@router.message(F.text & ~F.text.startswith("/"))
async def chat(message: Message, bot: Bot):
    uid = message.from_user.id
    u = db.touch_user(uid, message.from_user.username)

    if u["banned"]:
        await message.answer("Доступ к боту ограничен.")
        return
    if not u["adult"]:
        await message.answer("Сначала нажми /start и подтверди возраст.")
        return

    text = message.text

    # пользователь говорит, что ему нет 18: блокируем сразу
    if safety.claims_minor(text):
        db.ban_user(uid)
        await message.answer("Этот бот только для совершеннолетних, поэтому я не могу продолжать. Береги себя 🙂")
        await notify_admins(bot, f"🚫 Автоблокировка: {who(message.from_user)} написал(а), что ему(ей) нет 18.")
        return

    # сексуальные темы про несовершеннолетних: отказ, со второго раза блокировка
    if safety.sexual_minor(text):
        if db.add_strike(uid) >= 2:
            db.ban_user(uid)
            await message.answer("Доступ закрыт.")
            await notify_admins(bot, f"🚫 Автоблокировка: {who(message.from_user)}, повторные сообщения о несовершеннолетних.")
        else:
            await message.answer("С такими темами я не помогаю. Все персонажи только взрослые.")
        return

    c = db.get_character(u["character"])
    if not c:
        await message.answer("Сначала выбери персонажа:", reply_markup=characters_keyboard())
        return
    if not await is_subscribed(bot, uid):
        await message.answer("Чтобы болтать с персонажами, подпишись на канал 👇", reply_markup=subscribe_keyboard())
        return

    limit = 0 if uid in ADMIN_IDS else db.daily_limit()
    if not db.take_quota(uid, limit):
        await message.answer(f"На сегодня лимит ({limit} сообщений) закончился. Возвращайся завтра 🙂")
        return

    user_msg_id = db.add_message(uid, "user", text[:2000])
    system = BASE_PROMPT + f"Имя: {c['name']}.\n{c['persona']}" + safety.rules_suffix(bool(u["nsfw"]) and db.nsfw_allowed_globally())

    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            reply = await llm.generate(
                system, db.get_history(uid, HISTORY_LIMIT), nsfw=bool(u["nsfw"]) and db.nsfw_allowed_globally()
            )
        if not reply:
            raise ValueError("пустой ответ модели")
    except Exception as e:
        log.exception("Ошибка при запросе к модели: %s", e)
        db.delete_message(user_msg_id)
        db.refund_quota(uid)
        if getattr(e, "status_code", None) == 429:
            await message.answer("Сейчас много запросов, бесплатный лимит API исчерпан. Попробуй через минуту 🙏")
        else:
            await message.answer("Ой, что-то пошло не так. Попробуй написать ещё раз через минутку 🙏")
        return

    db.add_message(uid, "assistant", reply)
    await message.answer(reply[:4000])


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Заполни файл .env")
    if not llm.PROVIDERS:
        raise SystemExit("Не заданы LLM_API_KEY и LLM_MODEL. Заполни файл .env")
    log.info("Провайдеров модели в цепочке: %d", len(llm.PROVIDERS))
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст: админка недоступна. Впиши свой Telegram ID в .env")

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(admin.router)  # админка первой, чтобы ввод в её диалогах не уходил в чат с персонажем
    dp.include_router(router)
    await bot.set_my_commands(
        [
            BotCommand(command="characters", description="Выбрать персонажа"),
            BotCommand(command="mode", description="Режим 18+"),
            BotCommand(command="reset", description="Очистить память диалога"),
            BotCommand(command="help", description="Помощь"),
        ]
    )
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
