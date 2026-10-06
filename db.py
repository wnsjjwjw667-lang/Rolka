import sqlite3
from datetime import date

from characters import CHARACTERS as DEFAULT_CHARACTERS
from config import DAILY_LIMIT_DEFAULT, DB_PATH

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row

CHAR_FIELDS = ("name", "emoji", "emoji_id", "tagline", "greeting", "persona")


def _row(r):
    return dict(r) if r is not None else None


def _init() -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            adult INTEGER DEFAULT 0,
            character TEXT,
            day TEXT,
            used INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0,
            strikes INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT
        );
        CREATE TABLE IF NOT EXISTS characters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            emoji TEXT NOT NULL DEFAULT '🙂',
            tagline TEXT NOT NULL DEFAULT '',
            greeting TEXT NOT NULL,
            persona TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # миграция со старой версии базы
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for col, ddl in (("username", "TEXT"), ("banned", "INTEGER DEFAULT 0"), ("strikes", "INTEGER DEFAULT 0"), ("nsfw", "INTEGER DEFAULT 0"), ("custom_limit", "INTEGER")):
        if col not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")

    # миграция: id премиум-эмодзи персонажа (для иконки на кнопке)
    ccols = {r["name"] for r in conn.execute("PRAGMA table_info(characters)")}
    if "emoji_id" not in ccols:
        conn.execute("ALTER TABLE characters ADD COLUMN emoji_id TEXT DEFAULT ''")

    # стартовые персонажи из characters.py кладутся один раз
    if get_setting("seeded") is None:
        for c in DEFAULT_CHARACTERS.values():
            conn.execute(
                "INSERT INTO characters (name, emoji, tagline, greeting, persona) VALUES (?,?,?,?,?)",
                (c["name"], c["emoji"], c["tagline"], c["greeting"], c["persona"]),
            )
        set_setting("seeded", "1")
    conn.commit()


# ---------- настройки ----------

def get_setting(key: str, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()


def daily_limit() -> int:
    try:
        return int(get_setting("daily_limit", DAILY_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        return DAILY_LIMIT_DEFAULT


def user_limit(uid: int) -> int:
    """Лимит конкретного человека: личный, если задан админом, иначе общий. 0 = без лимита."""
    r = conn.execute("SELECT custom_limit FROM users WHERE user_id=?", (uid,)).fetchone()
    if r is not None and r["custom_limit"] is not None:
        return int(r["custom_limit"])
    return daily_limit()


def set_user_limit(uid: int, value: int | None) -> None:
    """value=None сбрасывает личный лимит (человек снова живёт по общему)."""
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    conn.execute("UPDATE users SET custom_limit=? WHERE user_id=?", (value, uid))
    conn.commit()


def adjust_user_limit(uid: int, delta: int) -> int:
    """Прибавляет или убавляет лимит от текущего значения человека. Возвращает новый лимит."""
    current = user_limit(uid)
    if current <= 0:  # сейчас без лимита: отталкиваемся от общего
        current = max(daily_limit(), 0)
    new = max(current + delta, 1)
    set_user_limit(uid, new)
    return new


def custom_limits() -> list[dict]:
    rows = conn.execute(
        "SELECT user_id, username, custom_limit FROM users WHERE custom_limit IS NOT NULL ORDER BY user_id"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- пользователи ----------

def touch_user(uid: int, username: str | None) -> dict:
    conn.execute(
        "INSERT INTO users (user_id, username) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
        (uid, username),
    )
    conn.commit()
    return _row(conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone())


def get_user(uid: int):
    return _row(conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone())


def set_adult(uid: int) -> None:
    conn.execute("UPDATE users SET adult=1 WHERE user_id=?", (uid,))
    conn.commit()


def set_nsfw(uid: int, on: bool) -> None:
    conn.execute("UPDATE users SET nsfw=? WHERE user_id=?", (1 if on else 0, uid))
    conn.commit()


def nsfw_allowed_globally() -> bool:
    """Общий выключатель режима 18+ для всего бота (админ). По умолчанию включён."""
    return get_setting("nsfw_allowed", "1") == "1"


def ban_user(uid: int, banned: bool = True) -> None:
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    conn.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if banned else 0, uid))
    if not banned:
        conn.execute("UPDATE users SET strikes=0 WHERE user_id=?", (uid,))
    conn.commit()


def add_strike(uid: int) -> int:
    conn.execute("UPDATE users SET strikes = strikes + 1 WHERE user_id=?", (uid,))
    conn.commit()
    return conn.execute("SELECT strikes FROM users WHERE user_id=?", (uid,)).fetchone()["strikes"]


def take_quota(uid: int, limit: int) -> bool:
    """Списывает одно сообщение из дневного лимита. limit <= 0 значит без лимита."""
    if limit <= 0:
        return True
    today = date.today().isoformat()
    u = get_user(uid)
    used = u["used"] if u and u["day"] == today else 0
    if used >= limit:
        return False
    conn.execute("UPDATE users SET day=?, used=? WHERE user_id=?", (today, used + 1, uid))
    conn.commit()
    return True


def refund_quota(uid: int) -> None:
    today = date.today().isoformat()
    conn.execute("UPDATE users SET used = MAX(used - 1, 0) WHERE user_id=? AND day=?", (uid, today))
    conn.commit()


def stats() -> dict:
    today = date.today().isoformat()

    def q(sql, *args):
        return conn.execute(sql, args).fetchone()[0]

    return {
        "users": q("SELECT COUNT(*) FROM users"),
        "adults": q("SELECT COUNT(*) FROM users WHERE adult=1"),
        "banned": q("SELECT COUNT(*) FROM users WHERE banned=1"),
        "active_today": q("SELECT COUNT(*) FROM users WHERE day=? AND used>0", today),
        "messages_today": q("SELECT COALESCE(SUM(used), 0) FROM users WHERE day=?", today),
    }


def recent_users(n: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT user_id, username, used, day, banned, custom_limit FROM users ORDER BY rowid DESC LIMIT ?", (n,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- история диалога ----------

def set_character(uid: int, char_id: int) -> None:
    conn.execute("UPDATE users SET character=? WHERE user_id=?", (str(char_id), uid))
    conn.execute("DELETE FROM messages WHERE user_id=?", (uid,))
    conn.commit()


def clear_history(uid: int) -> None:
    conn.execute("DELETE FROM messages WHERE user_id=?", (uid,))
    conn.commit()


def add_message(uid: int, role: str, content: str) -> int:
    cur = conn.execute("INSERT INTO messages (user_id, role, content) VALUES (?,?,?)", (uid, role, content))
    # храним не больше 100 последних сообщений на человека
    conn.execute(
        "DELETE FROM messages WHERE user_id=? AND id NOT IN "
        "(SELECT id FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 100)",
        (uid, uid),
    )
    conn.commit()
    return cur.lastrowid


def delete_message(msg_id: int) -> None:
    conn.execute("DELETE FROM messages WHERE id=?", (msg_id,))
    conn.commit()


def get_history(uid: int, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)
    ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


# ---------- персонажи ----------

def list_characters() -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM characters ORDER BY id")]


def get_character(cid):
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return None
    return _row(conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone())


def add_character(name: str, emoji: str, tagline: str, greeting: str, persona: str, emoji_id: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO characters (name, emoji, emoji_id, tagline, greeting, persona) VALUES (?,?,?,?,?,?)",
        (name, emoji, emoji_id or "", tagline, greeting, persona),
    )
    conn.commit()
    return cur.lastrowid


def update_character(cid: int, field: str, value: str) -> None:
    if field not in CHAR_FIELDS:
        raise ValueError(f"unknown field {field}")
    conn.execute(f"UPDATE characters SET {field}=? WHERE id=?", (value, cid))
    conn.commit()


def delete_character(cid: int) -> None:
    conn.execute("DELETE FROM characters WHERE id=?", (cid,))
    conn.execute("UPDATE users SET character=NULL WHERE character=?", (str(cid),))
    conn.commit()


_init()
