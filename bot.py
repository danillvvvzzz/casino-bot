import asyncio
import time
import datetime as dt
from typing import Optional, Tuple, List

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# =========================
# НАСТРОЙКИ
# =========================
TOKEN = "8035491816:AAE3SpY0JHEMq2tR82b9eXss6r0fUo2V1Mw"  # <-- вставь токен сюда

DB_PATH = "casino.db"

CASINO_EMOJI = "🎰"                # Telegram dice emoji (slot machine)
DEFAULT_COOLDOWN = 60 * 60         # 1 час
WARN_AUTO_DELETE_SEC = 10          # удалять предупреждение через 10 секунд
BOSS_TITLE = "Казино-босс недели"  # кастомный титул (Telegram даёт ставить только админам)


# =========================
# ВСПОМОГАТЕЛЬНОЕ
# =========================
def week_key(d: Optional[dt.date] = None) -> str:
    """Напр. '2026-W08' (ISO week)."""
    if d is None:
        d = dt.date.today()
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            cooldown_sec INTEGER NOT NULL DEFAULT 3600
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS usage_weekly (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            week TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id, week)
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS last_used (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            last_ts REAL NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS week_state (
            chat_id INTEGER PRIMARY KEY,
            current_week TEXT NOT NULL
        )
        """)
        await db.commit()


async def get_cooldown(db: aiosqlite.Connection, chat_id: int) -> int:
    cur = await db.execute("SELECT cooldown_sec FROM settings WHERE chat_id = ?", (chat_id,))
    row = await cur.fetchone()
    await cur.close()
    if row:
        return int(row[0])

    await db.execute(
        "INSERT OR IGNORE INTO settings(chat_id, cooldown_sec) VALUES(?, ?)",
        (chat_id, DEFAULT_COOLDOWN),
    )
    await db.commit()
    return DEFAULT_COOLDOWN


async def set_cooldown(db: aiosqlite.Connection, chat_id: int, cooldown_sec: int) -> None:
    await db.execute(
        "INSERT INTO settings(chat_id, cooldown_sec) VALUES(?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET cooldown_sec=excluded.cooldown_sec",
        (chat_id, cooldown_sec),
    )
    await db.commit()


async def get_last_used(db: aiosqlite.Connection, chat_id: int, user_id: int) -> Optional[float]:
    cur = await db.execute(
        "SELECT last_ts FROM last_used WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    row = await cur.fetchone()
    await cur.close()
    return float(row[0]) if row else None


async def set_last_used(db: aiosqlite.Connection, chat_id: int, user_id: int, ts: float) -> None:
    await db.execute(
        "INSERT INTO last_used(chat_id, user_id, last_ts) VALUES(?, ?, ?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET last_ts=excluded.last_ts",
        (chat_id, user_id, ts),
    )
    await db.commit()


async def inc_weekly(db: aiosqlite.Connection, chat_id: int, user_id: int, wk: str, delta: int = 1) -> None:
    await db.execute(
        "INSERT INTO usage_weekly(chat_id, user_id, week, count) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(chat_id, user_id, week) DO UPDATE SET count = count + ?",
        (chat_id, user_id, wk, delta, delta),
    )
    await db.commit()


async def get_week_leader(db: aiosqlite.Connection, chat_id: int, wk: str) -> Optional[Tuple[int, int]]:
    cur = await db.execute(
        "SELECT user_id, count FROM usage_weekly WHERE chat_id = ? AND week = ? ORDER BY count DESC LIMIT 1",
        (chat_id, wk),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return None
    return int(row[0]), int(row[1])


async def get_week_top(db: aiosqlite.Connection, chat_id: int, wk: str, limit: int = 5) -> List[Tuple[int, int]]:
    cur = await db.execute(
        "SELECT user_id, count FROM usage_weekly WHERE chat_id = ? AND week = ? ORDER BY count DESC LIMIT ?",
        (chat_id, wk, limit),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [(int(u), int(c)) for (u, c) in rows]


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in ("administrator", "creator")


async def ensure_week_rollover(bot: Bot, db: aiosqlite.Connection, chat_id: int) -> Optional[str]:
    """
    Если неделя сменилась — возвращает текст объявления победителя прошлой недели,
    и обновляет state. Иначе None.
    """
    current = week_key()

    cur = await db.execute("SELECT current_week FROM week_state WHERE chat_id = ?", (chat_id,))
    row = await cur.fetchone()
    await cur.close()

    if not row:
        await db.execute("INSERT INTO week_state(chat_id, current_week) VALUES(?, ?)", (chat_id, current))
        await db.commit()
        return None

    prev_week = row[0]
    if prev_week == current:
        return None

    leader = await get_week_leader(db, chat_id, prev_week)

    text = f"📅 Неделя сменилась: <b>{prev_week}</b> → <b>{current}</b>\n"
    if not leader:
        text += "На прошлой неделе никто не крутил 🎰. Скучно 😄"
    else:
        user_id, count = leader
        text += f"👑 <b>Казино-босс недели:</b> <a href='tg://user?id={user_id}'>игрок</a> — <b>{count}</b> 🎰"

        # Титул Telegram можно ставить только участнику-админу
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status in ("administrator", "creator"):
                await bot.set_chat_administrator_custom_title(
                    chat_id=chat_id,
                    user_id=user_id,
                    custom_title=BOSS_TITLE
                )
                text += f"\n✨ Титул установлен: <b>{BOSS_TITLE}</b>"
            else:
                text += "\nℹ️ Титул Telegram ставится только админам — корона остаётся символической 👑"
        except (TelegramBadRequest, TelegramForbiddenError):
            text += "\n⚠️ Не смог поставить титул (возможно нет прав у бота)."

    await db.execute("UPDATE week_state SET current_week = ? WHERE chat_id = ?", (current, chat_id))
    await db.commit()
    return text


# =========================
# ОСНОВНОЙ КОД БОТА
# =========================
async def main():
    await init_db()

    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()

    # ----- команды -----
    @dp.message(F.text == "/boss")
    async def cmd_boss(message: Message):
        chat_id = message.chat.id
        async with aiosqlite.connect(DB_PATH) as db:
            await ensure_week_rollover(bot, db, chat_id)
            wk = week_key()
            leader = await get_week_leader(db, chat_id, wk)

        if not leader:
            await message.answer("Пока нет лидера недели — никто не крутил 🎰.")
            return

        user_id, count = leader
        await message.answer(f"👑 Лидер недели <b>{wk}</b>: <a href='tg://user?id={user_id}'>игрок</a> — <b>{count}</b> 🎰")

    @dp.message(F.text == "/top")
    async def cmd_top(message: Message):
        chat_id = message.chat.id
        async with aiosqlite.connect(DB_PATH) as db:
            await ensure_week_rollover(bot, db, chat_id)
            wk = week_key()
            top = await get_week_top(db, chat_id, wk, limit=5)

        if not top:
            await message.answer("Топ пуст — никто не крутил 🎰 на этой неделе.")
            return

        lines = [f"🏆 Топ 🎰 за неделю <b>{wk}</b>:"]
        for i, (uid, cnt) in enumerate(top, start=1):
            lines.append(f"{i}) <a href='tg://user?id={uid}'>игрок</a> — <b>{cnt}</b>")
        await message.answer("\n".join(lines))

    @dp.message(F.text.startswith("/cooldown"))
    async def cmd_cooldown(message: Message):
        chat_id = message.chat.id
        user_id = message.from_user.id

        if not await is_admin(bot, chat_id, user_id):
            await message.answer("Эта команда только для админов.")
            return

        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Использование: <code>/cooldown 3600</code> (секунды)")
            return

        cooldown_sec = int(parts[1])
        if cooldown_sec < 10 or cooldown_sec > 24 * 60 * 60:
            await message.answer("Поставь значение от 10 до 86400 секунд.")
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await set_cooldown(db, chat_id, cooldown_sec)

        await message.answer(f"✅ Кулдаун для 🎰 теперь: <b>{cooldown_sec}</b> сек.")

    # ----- лимитер для Telegram dice 🎰 -----
    @dp.message(F.dice)
    async def casino_limiter(message: Message):
        if not message.dice or message.dice.emoji != CASINO_EMOJI:
            return

        chat_id = message.chat.id
        user_id = message.from_user.id if message.from_user else None
        if user_id is None:
            return

        async with aiosqlite.connect(DB_PATH) as db:
            rollover_text = await ensure_week_rollover(bot, db, chat_id)
            if rollover_text:
                await message.answer(rollover_text)

            cooldown = await get_cooldown(db, chat_id)
            now = time.time()
            prev = await get_last_used(db, chat_id, user_id)

            if prev is not None and (now - prev) < cooldown:
                remaining = int(cooldown - (now - prev))
                mins, secs = divmod(remaining, 60)

                try:
                    await message.delete()
                except Exception:
                    # если не можем удалить — просто предупредим
                    pass

                warn = await message.answer(
                    f"⛔️ 🎰 можно только раз в час.\n"
                    f"Осталось: <b>{mins}м {secs}с</b>"
                )
                await asyncio.sleep(WARN_AUTO_DELETE_SEC)
                try:
                    await warn.delete()
                except Exception:
                    pass
                return

            # разрешено
            await set_last_used(db, chat_id, user_id, now)
            await inc_weekly(db, chat_id, user_id, week_key(), delta=1)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())