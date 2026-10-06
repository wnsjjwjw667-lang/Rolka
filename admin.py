"""Админка внутри Telegram. Доступна только ID из ADMIN_IDS. Открывается командой /admin."""

import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import safety
from config import ADMIN_IDS

router = Router()
router.message.filter(F.chat.type == "private", F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

# поле: (название, максимум символов)
FIELDS = {
    "name": ("Имя", 40),
    "emoji": ("Эмодзи", 8),
    "tagline": ("Описание для меню", 120),
    "greeting": ("Приветствие", 500),
    "persona": ("Характер", 1500),
}

PANEL_TEXT = (
    "🛠 <b>Админка</b>\n\n"
    "Команды:\n"
    "/limit 150 — общий лимит сообщений в день (0 = без лимита)\n"
    "/ulimit ID 300 — личный лимит человеку\n"
    "/ulimit ID +50 — добавить ему 50, /ulimit ID -30 — убавить на 30\n"
    "/ulimit ID reset — вернуть общий лимит\n"
    "/ulimits — у кого заданы личные лимиты\n"
    "/users — последние пользователи\n"
    "/ban ID и /unban ID — блокировка\n"
    "/stats — статистика\n"
    "/cancel — отменить ввод"
)


class AddChar(StatesGroup):
    name = State()
    emoji = State()
    tagline = State()
    greeting = State()
    persona = State()


class EditChar(StatesGroup):
    value = State()


def panel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Персонажи", callback_data="adm:list")
    kb.button(text="➕ Добавить персонажа", callback_data="adm:add")
    kb.button(text="📊 Статистика", callback_data="adm:stats")
    kb.adjust(1)
    return kb.as_markup()


def card_text(c: dict) -> str:
    return (
        f"{html.escape(c['emoji'])} <b>{html.escape(c['name'])}</b> (id {c['id']})\n\n"
        f"<b>Описание:</b> {html.escape(c['tagline'])}\n\n"
        f"<b>Приветствие:</b> {html.escape(c['greeting'])}\n\n"
        f"<b>Характер:</b> {html.escape(c['persona'])}"
    )


def card_kb(cid: int):
    kb = InlineKeyboardBuilder()
    for field, (label, _) in FIELDS.items():
        kb.button(text=f"✏️ {label}", callback_data=f"adm:e:{cid}:{field}")
    kb.button(text="🗑 Удалить", callback_data=f"adm:d:{cid}")
    kb.button(text="⬅️ Назад", callback_data="adm:list")
    kb.adjust(2)
    return kb.as_markup()


def fmt_limit(n: int) -> str:
    return "без лимита" if n <= 0 else f"{n} сообщений в день на человека"


def stats_text() -> str:
    s = db.stats()
    return (
        "📊 <b>Статистика</b>\n\n"
        f"Пользователей: {s['users']}\n"
        f"Подтвердили 18+: {s['adults']}\n"
        f"Активны сегодня: {s['active_today']}\n"
        f"Сообщений сегодня: {s['messages_today']}\n"
        f"Заблокировано: {s['banned']}\n"
        f"Лимит: {fmt_limit(db.daily_limit())}"
    )


def validate(field: str, text: str) -> str | None:
    _, limit = FIELDS[field]
    if not text:
        return "Пусто. Пришли текст."
    if len(text) > limit:
        return f"Слишком длинно: {len(text)} из {limit}. Сократи и пришли снова."
    if safety.looks_underage(text):
        return "Персонажи только взрослые (18+). Убери возраст младше 18 и слова про детей или школьников и пришли снова."
    if field == "persona" and not safety.adult_age_stated(text):
        return "Укажи в описании возраст персонажа 18 или больше, например «27 лет», и пришли снова."
    return None


# ---------- команды (должны быть выше обработчиков состояний) ----------

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(PANEL_TEXT, reply_markup=panel_kb(), parse_mode="HTML")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. /admin — панель.")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    await message.answer(stats_text(), parse_mode="HTML")


@router.message(Command("limit"))
async def cmd_limit(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer(
            f"Сейчас: {fmt_limit(db.daily_limit())}.\nЧтобы поменять: /limit 150 (0 = без лимита)"
        )
        return
    db.set_setting("daily_limit", int(arg))
    await message.answer(f"✅ Лимит теперь: {fmt_limit(int(arg))}")


@router.message(Command("ulimit"))
async def cmd_ulimit(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    usage = (
        "Формат:\n"
        "/ulimit 123456789 — посмотреть\n"
        "/ulimit 123456789 300 — задать личный лимит (0 = без лимита)\n"
        "/ulimit 123456789 +50 — добавить\n"
        "/ulimit 123456789 -30 — убавить\n"
        "/ulimit 123456789 reset — вернуть общий лимит"
    )
    if not parts or not parts[0].isdigit() or len(parts) > 2:
        await message.answer(usage)
        return
    uid = int(parts[0])
    if len(parts) == 1:
        u = db.get_user(uid)
        if not u:
            await message.answer("Такого пользователя в базе нет.")
            return
        kind = "личный" if u.get("custom_limit") is not None else "общий"
        await message.answer(f"Пользователь {uid}: {fmt_limit(db.user_limit(uid))} ({kind}).")
        return
    arg = parts[1].lower()
    if arg in ("reset", "off", "сброс"):
        db.set_user_limit(uid, None)
        await message.answer(f"✅ {uid}: личный лимит снят, теперь общий: {fmt_limit(db.daily_limit())}")
    elif arg[0] in "+-" and arg[1:].isdigit():
        new = db.adjust_user_limit(uid, int(arg))
        await message.answer(f"✅ {uid}: теперь {fmt_limit(new)}")
    elif arg.isdigit():
        db.set_user_limit(uid, int(arg))
        await message.answer(f"✅ {uid}: теперь {fmt_limit(int(arg))}")
    else:
        await message.answer(usage)


@router.message(Command("ulimits"))
async def cmd_ulimits(message: Message):
    rows = db.custom_limits()
    if not rows:
        await message.answer("Личных лимитов нет, у всех общий.")
        return
    lines = []
    for r in rows:
        name = f"@{r['username']}" if r["username"] else "без username"
        lines.append(f"{r['user_id']} · {name} · {fmt_limit(r['custom_limit'])}")
    await message.answer("Личные лимиты:\n\n" + "\n".join(lines))


@router.message(Command("users"))
async def cmd_users(message: Message):
    rows = db.recent_users(20)
    if not rows:
        await message.answer("Пользователей пока нет.")
        return
    lines = []
    for r in rows:
        name = f"@{r['username']}" if r["username"] else "без username"
        mark = " 🚫" if r["banned"] else ""
        lim = f" · лимит {r['custom_limit']}" if r.get("custom_limit") is not None else ""
        lines.append(f"{r['user_id']} · {name} · сообщений: {r['used'] or 0}{lim}{mark}")
    await message.answer("Последние пользователи:\n\n" + "\n".join(lines))


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /ban 123456789")
        return
    uid = int(arg)
    if uid in ADMIN_IDS:
        await message.answer("Это админ, его заблокировать нельзя.")
        return
    db.ban_user(uid, True)
    await message.answer(f"🚫 Пользователь {uid} заблокирован.")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /unban 123456789")
        return
    db.ban_user(int(arg), False)
    await message.answer(f"✅ Пользователь {arg} разблокирован.")


# ---------- кнопки ----------

@router.callback_query(F.data == "adm:home")
async def cb_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(PANEL_TEXT, reply_markup=panel_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "adm:stats")
async def cb_stats(call: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="adm:home")
    await call.message.edit_text(stats_text(), reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "adm:list")
async def cb_list(call: CallbackQuery, state: FSMContext):
    await state.clear()
    chars = db.list_characters()
    kb = InlineKeyboardBuilder()
    for c in chars:
        kb.button(text=f"{c['emoji']} {c['name']}", callback_data=f"adm:c:{c['id']}")
    kb.button(text="➕ Добавить", callback_data="adm:add")
    kb.button(text="⬅️ Назад", callback_data="adm:home")
    kb.adjust(2)
    text = (
        "👥 <b>Персонажи</b>\nНажми на персонажа, чтобы изменить его."
        if chars
        else "Персонажей пока нет. Добавь первого."
    )
    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("adm:c:"))
async def cb_card(call: CallbackQuery, state: FSMContext):
    await state.clear()
    c = db.get_character(call.data.split(":")[2])
    if not c:
        await call.answer("Персонаж не найден", show_alert=True)
        return
    await call.message.edit_text(card_text(c), reply_markup=card_kb(c["id"]), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("adm:e:"))
async def cb_edit(call: CallbackQuery, state: FSMContext):
    _, _, cid, field = call.data.split(":")
    c = db.get_character(cid)
    if not c or field not in FIELDS:
        await call.answer("Не найдено", show_alert=True)
        return
    label, limit = FIELDS[field]
    await state.set_state(EditChar.value)
    await state.update_data(cid=c["id"], field=field)
    await call.message.answer(
        f"Пришли новое значение для поля «{label}» (до {limit} символов).\n\n"
        f"Сейчас:\n{html.escape(c[field])}\n\n/cancel — отмена",
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:d:"))
async def cb_delete(call: CallbackQuery):
    cid = call.data.split(":")[2]
    c = db.get_character(cid)
    if not c:
        await call.answer("Персонаж не найден", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить", callback_data=f"adm:dy:{c['id']}")
    kb.button(text="Отмена", callback_data=f"adm:c:{c['id']}")
    kb.adjust(2)
    await call.message.edit_text(
        f"Удалить персонажа <b>{html.escape(c['name'])}</b>? Это нельзя отменить.",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:dy:"))
async def cb_delete_yes(call: CallbackQuery):
    db.delete_character(int(call.data.split(":")[2]))
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 К списку", callback_data="adm:list")
    await call.message.edit_text("🗑 Удалено.", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "adm:add")
async def cb_add(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddChar.name)
    await call.message.answer(
        "Новый персонаж. Все персонажи вымышленные и взрослые (18+).\n\n"
        "Шаг 1/5. Пришли <b>имя</b>.\n\n/cancel — отмена",
        parse_mode="HTML",
    )
    await call.answer()


# ---------- ввод значений ----------

@router.message(EditChar.value, F.text)
async def edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    field, cid = data["field"], data["cid"]
    text = message.text.strip()
    err = validate(field, text)
    if err:
        await message.answer(err)
        return
    db.update_character(cid, field, text)
    await state.clear()
    c = db.get_character(cid)
    await message.answer("✅ Сохранено.\n\n" + card_text(c), reply_markup=card_kb(cid), parse_mode="HTML")


@router.message(AddChar.name, F.text)
async def add_name(message: Message, state: FSMContext):
    text = message.text.strip()
    err = validate("name", text)
    if err:
        await message.answer(err)
        return
    await state.update_data(name=text)
    await state.set_state(AddChar.emoji)
    await message.answer("Шаг 2/5. Пришли <b>эмодзи</b> для кнопки, например 🎸", parse_mode="HTML")


@router.message(AddChar.emoji, F.text)
async def add_emoji(message: Message, state: FSMContext):
    text = message.text.strip()
    err = validate("emoji", text)
    if err:
        await message.answer(err)
        return
    await state.update_data(emoji=text)
    await state.set_state(AddChar.tagline)
    await message.answer(
        "Шаг 3/5. Пришли <b>короткое описание</b> для меню, например: Бариста, 26. Добрый и с юмором.",
        parse_mode="HTML",
    )


@router.message(AddChar.tagline, F.text)
async def add_tagline(message: Message, state: FSMContext):
    text = message.text.strip()
    err = validate("tagline", text)
    if err:
        await message.answer(err)
        return
    await state.update_data(tagline=text)
    await state.set_state(AddChar.greeting)
    await message.answer("Шаг 4/5. Пришли <b>приветствие</b>: первую реплику персонажа.", parse_mode="HTML")


@router.message(AddChar.greeting, F.text)
async def add_greeting(message: Message, state: FSMContext):
    text = message.text.strip()
    err = validate("greeting", text)
    if err:
        await message.answer(err)
        return
    await state.update_data(greeting=text)
    await state.set_state(AddChar.persona)
    await message.answer(
        "Шаг 5/5. Опиши <b>характер и манеру речи</b>: кто он, чем живёт, как говорит. "
        "Обязательно укажи возраст 18+, например «27 лет».",
        parse_mode="HTML",
    )


@router.message(AddChar.persona, F.text)
async def add_persona(message: Message, state: FSMContext):
    text = message.text.strip()
    err = validate("persona", text)
    if err:
        await message.answer(err)
        return
    data = await state.get_data()
    cid = db.add_character(data["name"], data["emoji"], data["tagline"], data["greeting"], text)
    await state.clear()
    c = db.get_character(cid)
    await message.answer(
        "✅ Персонаж добавлен, пользователи уже видят его в меню.\n\n" + card_text(c),
        reply_markup=card_kb(cid),
        parse_mode="HTML",
    )
