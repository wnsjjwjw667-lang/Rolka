import os

from dotenv import load_dotenv

load_dotenv()


def _ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Telegram ID админов через запятую. Свой ID можно узнать у бота @userinfobot
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", ""))

# Стартовый лимит сообщений в день на человека. Потом меняется в боте командой /limit
DAILY_LIMIT_DEFAULT = int(os.getenv("DAILY_LIMIT", "100"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/Recenp")

DB_PATH = os.getenv("DB_PATH", "bot.db")
