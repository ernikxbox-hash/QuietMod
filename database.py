"""
database.py — весь слой данных (aiosqlite)

Используется ОДНО постоянное соединение на весь процесс (открывается в init_db,
закрывается в close_db), а не новое соединение на каждый запрос — так SQLite
не ловит "database is locked" при параллельных операциях, и каждый запрос
не платит цену открытия файла заново.
"""
import asyncio
import aiosqlite
import logging
from datetime import datetime, date
from typing import Optional

DB_PATH = "data/bot.db"
log = logging.getLogger("db")

# Единое соединение на процесс + лок на запись.
# SQLite допускает только одного writer'а одновременно — лок не даёт
# двум корутинам столкнуться на INSERT/UPDATE/DELETE в одно и то же время.
_conn: Optional[aiosqlite.Connection] = None
_write_lock = asyncio.Lock()


def _get_conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("database.init_db() must be called before using the database")
    return _conn


# ──────────────────────────────────────────────
#  INIT / SHUTDOWN
# ──────────────────────────────────────────────
async def init_db():
    global _conn
    import os
    os.makedirs("data", exist_ok=True)

    _conn = await aiosqlite.connect(DB_PATH)
    _conn.row_factory = aiosqlite.Row

    await _conn.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;

    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY,
        username    TEXT,
        full_name   TEXT,
        referrer_id INTEGER,
        premium_until TEXT,           -- ISO date str, NULL = нет
        donor_badge INTEGER DEFAULT 0,-- 1 = есть значок
        joined      TEXT NOT NULL,
        ai_calls_today INTEGER DEFAULT 0,  -- зарезервировано: лимит ИИ сейчас отключён (безлимит для всех)
        ai_date     TEXT                   -- зарезервировано: дата сброса счётчика, когда лимит включат обратно
    );

    CREATE TABLE IF NOT EXISTS messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id    INTEGER NOT NULL,
        msg_id      INTEGER NOT NULL,
        sender_id   INTEGER,
        from_name   TEXT,
        username    TEXT,
        chat        TEXT,
        date        TEXT,
        text        TEXT,
        media_type  TEXT,
        file_id     TEXT,
        created_at  TEXT NOT NULL,
        UNIQUE(owner_id, msg_id)
    );

    CREATE TABLE IF NOT EXISTS payments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        stars       INTEGER NOT NULL,
        payload     TEXT,
        created_at  TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS ideas (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        username    TEXT,
        full_name   TEXT,
        text        TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS saved_messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id    INTEGER NOT NULL,
        from_name   TEXT,
        username    TEXT,
        chat        TEXT,
        date        TEXT,
        text        TEXT,
        media_type  TEXT,
        file_id     TEXT,
        event_type  TEXT NOT NULL,
        old_text    TEXT,
        saved_at    TEXT NOT NULL,
        expires_at  TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_messages_owner ON messages(owner_id);
    CREATE INDEX IF NOT EXISTS idx_messages_owner_msg ON messages(owner_id, msg_id);
    CREATE INDEX IF NOT EXISTS idx_saved_owner ON saved_messages(owner_id);
    """)
    await _conn.commit()

    # Миграция: добавляем sender_id если ещё нет (для существующих БД)
    try:
        await _conn.execute("ALTER TABLE messages ADD COLUMN sender_id INTEGER")
        await _conn.commit()
        log.info("🔧 Миграция: добавлена колонка sender_id")
    except Exception:
        pass  # уже есть

    log.info("✅ DB инициализирована")


async def close_db():
    """Закрывает соединение с БД — вызывать при остановке бота."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None
        log.info("🔒 DB соединение закрыто")


# ──────────────────────────────────────────────
#  USERS
# ──────────────────────────────────────────────
async def get_user(uid: int) -> Optional[dict]:
    db = _get_conn()
    async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def upsert_user(uid: int, username: str, full_name: str, referrer_id: Optional[int] = None):
    now = datetime.now().isoformat()
    db = _get_conn()
    async with _write_lock:
        await db.execute("""
            INSERT INTO users (id, username, full_name, referrer_id, joined)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username  = excluded.username,
                full_name = excluded.full_name
        """, (uid, username, full_name, referrer_id, now))
        await db.commit()


async def set_premium(uid: int, until: date):
    db = _get_conn()
    async with _write_lock:
        await db.execute(
            "UPDATE users SET premium_until=? WHERE id=?",
            (until.isoformat(), uid)
        )
        await db.commit()


async def set_donor_badge(uid: int):
    db = _get_conn()
    async with _write_lock:
        await db.execute("UPDATE users SET donor_badge=1 WHERE id=?", (uid,))
        await db.commit()


async def is_premium(uid: int) -> bool:
    user = await get_user(uid)
    if not user or not user["premium_until"]:
        return False
    return date.fromisoformat(user["premium_until"]) >= date.today()


async def count_users() -> int:
    db = _get_conn()
    async with db.execute("SELECT COUNT(*) FROM users") as cur:
        return (await cur.fetchone())[0]


async def count_referrals(uid: int) -> int:
    db = _get_conn()
    async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (uid,)) as cur:
        return (await cur.fetchone())[0]


async def all_user_ids() -> list[int]:
    db = _get_conn()
    async with db.execute("SELECT id FROM users") as cur:
        return [r[0] for r in await cur.fetchall()]


async def get_all_users(limit: int = 50, offset: int = 0) -> list[dict]:
    """Список пользователей с username/full_name/referrer_id, новые сверху."""
    db = _get_conn()
    async with db.execute(
        "SELECT id, username, full_name, referrer_id, joined FROM users "
        "ORDER BY joined DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ──────────────────────────────────────────────
#  MESSAGES CACHE (персистентный)
# ──────────────────────────────────────────────
FREE_CACHE_LIMIT    = 20
PREMIUM_CACHE_LIMIT = 200


async def save_message(owner_id: int, msg: dict):
    """
    Сохраняем/обновляем сообщение в кэше, при переполнении — удаляем самое старое.

    ВАЖНО: раньше здесь стоял INSERT OR IGNORE — из-за UNIQUE(owner_id, msg_id)
    повторный save_message() для уже закэшированного msg_id (например, при
    редактировании сообщения) молча игнорировался и текст в кэше не обновлялся.
    Из-за этого при втором и последующих редактированиях уведомление показывало
    протухшее "было" вместо реального предыдущего текста. UPSERT это чинит.
    """
    limit = PREMIUM_CACHE_LIMIT if await is_premium(owner_id) else FREE_CACHE_LIMIT
    now   = datetime.now().isoformat()
    db = _get_conn()
    async with _write_lock:
        await db.execute("""
            INSERT INTO messages
              (owner_id, msg_id, sender_id, from_name, username, chat, date, text, media_type, file_id, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(owner_id, msg_id) DO UPDATE SET
                sender_id  = excluded.sender_id,
                from_name  = excluded.from_name,
                username   = excluded.username,
                chat       = excluded.chat,
                date       = excluded.date,
                text       = excluded.text,
                media_type = excluded.media_type,
                file_id    = excluded.file_id
        """, (
            owner_id, msg["msg_id"], msg.get("sender_id"), msg["from_name"], msg["username"],
            msg["chat"], msg["date"], msg["text"],
            msg["media_type"], msg["file_id"], now
        ))
        # Обрезаем кэш
        await db.execute("""
            DELETE FROM messages
            WHERE owner_id=? AND id NOT IN (
                SELECT id FROM messages WHERE owner_id=?
                ORDER BY id DESC LIMIT ?
            )
        """, (owner_id, owner_id, limit))
        await db.commit()


async def get_message(owner_id: int, msg_id: int) -> Optional[dict]:
    db = _get_conn()
    async with db.execute(
        "SELECT * FROM messages WHERE owner_id=? AND msg_id=?",
        (owner_id, msg_id)
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_recent_messages(owner_id: int, limit: int = 20) -> list[dict]:
    db = _get_conn()
    async with db.execute(
        "SELECT * FROM messages WHERE owner_id=? ORDER BY id DESC LIMIT ?",
        (owner_id, limit)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def delete_message(owner_id: int, msg_id: int):
    db = _get_conn()
    async with _write_lock:
        await db.execute(
            "DELETE FROM messages WHERE owner_id=? AND msg_id=?",
            (owner_id, msg_id)
        )
        await db.commit()


async def clear_messages(owner_id: int) -> int:
    db = _get_conn()
    async with _write_lock:
        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE owner_id=?", (owner_id,)
        ) as cur:
            count = (await cur.fetchone())[0]
        await db.execute("DELETE FROM messages WHERE owner_id=?", (owner_id,))
        await db.commit()
    return count


async def count_messages(owner_id: int) -> int:
    db = _get_conn()
    async with db.execute(
        "SELECT COUNT(*) FROM messages WHERE owner_id=?", (owner_id,)
    ) as cur:
        return (await cur.fetchone())[0]


async def search_messages(owner_id: int, query: str) -> list[dict]:
    """Поиск по тексту — только для premium."""
    db = _get_conn()
    async with db.execute("""
        SELECT * FROM messages
        WHERE owner_id=? AND (text LIKE ? OR from_name LIKE ? OR username LIKE ?)
        ORDER BY id DESC LIMIT 30
    """, (owner_id, f"%{query}%", f"%{query}%", f"%{query}%")) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def total_messages_all() -> int:
    db = _get_conn()
    async with db.execute("SELECT COUNT(*) FROM messages") as cur:
        return (await cur.fetchone())[0]


# ──────────────────────────────────────────────
#  PAYMENTS
# ──────────────────────────────────────────────
async def save_payment(uid: int, stars: int, payload: str):
    db = _get_conn()
    async with _write_lock:
        await db.execute(
            "INSERT INTO payments (user_id, stars, payload, created_at) VALUES (?,?,?,?)",
            (uid, stars, payload, datetime.now().isoformat())
        )
        await db.commit()


async def total_stars() -> int:
    db = _get_conn()
    async with db.execute("SELECT COALESCE(SUM(stars),0) FROM payments") as cur:
        return (await cur.fetchone())[0]


# ──────────────────────────────────────────────
#  IDEAS
# ──────────────────────────────────────────────
async def save_idea(user_id: int, username: str, full_name: str, text: str):
    db = _get_conn()
    async with _write_lock:
        await db.execute(
            "INSERT INTO ideas (user_id, username, full_name, text, created_at) VALUES (?,?,?,?,?)",
            (user_id, username, full_name, text, datetime.now().isoformat())
        )
        await db.commit()


async def get_ideas(limit: int = 30) -> list[dict]:
    db = _get_conn()
    async with db.execute(
        "SELECT * FROM ideas ORDER BY id DESC LIMIT ?", (limit,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def delete_idea(idea_id: int):
    db = _get_conn()
    async with _write_lock:
        await db.execute("DELETE FROM ideas WHERE id=?", (idea_id,))
        await db.commit()


async def clear_ideas():
    db = _get_conn()
    async with _write_lock:
        await db.execute("DELETE FROM ideas")
        await db.commit()


async def count_ideas() -> int:
    db = _get_conn()
    async with db.execute("SELECT COUNT(*) FROM ideas") as cur:
        return (await cur.fetchone())[0]


# ──────────────────────────────────────────────
#  SAVED MESSAGES (7-дневное хранилище)
# ──────────────────────────────────────────────
async def save_intercepted(owner_id: int, data: dict) -> int:
    """Сохраняет перехваченное сообщение в saved_messages на 7 дней.
    Возвращает id записи."""
    now = datetime.now()
    expires = now + __import__('datetime').timedelta(days=7)
    conn = _get_conn()
    async with _write_lock:
        cur = await conn.execute("""
            INSERT INTO saved_messages
              (owner_id, from_name, username, chat, date, text, media_type, file_id,
               event_type, old_text, saved_at, expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            owner_id, data.get("from_name"), data.get("username"),
            data.get("chat"), data.get("date"), data.get("text"),
            data.get("media_type"), data.get("file_id"),
            data.get("event_type", "deleted"), data.get("old_text"),
            now.isoformat(), expires.isoformat()
        ))
        await conn.commit()
        return cur.lastrowid


async def get_saved_messages(owner_id: int) -> list[dict]:
    """Возвращает сохранённые сообщения (ещё не истёкшие)."""
    conn = _get_conn()
    now = datetime.now().isoformat()
    async with conn.execute("""
        SELECT * FROM saved_messages
        WHERE owner_id=? AND expires_at > ?
        ORDER BY id DESC
    """, (owner_id, now)) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def delete_saved_message(save_id: int):
    conn = _get_conn()
    async with _write_lock:
        await conn.execute("DELETE FROM saved_messages WHERE id=?", (save_id,))
        await conn.commit()


async def count_saved_messages(owner_id: int) -> int:
    conn = _get_conn()
    now = datetime.now().isoformat()
    async with conn.execute(
        "SELECT COUNT(*) FROM saved_messages WHERE owner_id=? AND expires_at > ?",
        (owner_id, now)
    ) as cur:
        return (await cur.fetchone())[0]


async def purge_expired_saved():
    """Удаляет все истёкшие saved_messages — вызывать при старте или по расписанию."""
    conn = _get_conn()
    now = datetime.now().isoformat()
    async with _write_lock:
        await conn.execute("DELETE FROM saved_messages WHERE expires_at <= ?", (now,))
        await conn.commit()
