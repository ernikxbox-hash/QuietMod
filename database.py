import asyncio
import aiosqlite
import logging
from datetime import datetime, timezone
from typing import Optional

DB_PATH = "data/bot.db"
log = logging.getLogger("db")
_conn: Optional[aiosqlite.Connection] = None
_write_lock = asyncio.Lock()
_msg_write_queue: Optional[asyncio.Queue] = None
_msg_writer_task: Optional[asyncio.Task] = None
_owner_cleanup_counter: dict[int, int] = {}
_MSG_BATCH_MAX = 60
_MSG_BATCH_WAIT_SECONDS = 0.25
_OWNER_CLEANUP_EVERY = 25
_STATS_BATCH_MAX = 100
_STATS_BATCH_WAIT_SECONDS = 0.5
_stats_write_queue: Optional[asyncio.Queue] = None
_stats_writer_task: Optional[asyncio.Task] = None

def _ensure_msg_writer_started():
    global _msg_write_queue, _msg_writer_task
    if _msg_write_queue is None:
        _msg_write_queue = asyncio.Queue(maxsize=4000)
    if _msg_writer_task is None or _msg_writer_task.done():
        _msg_writer_task = asyncio.create_task(_msg_writer_loop())
def _ensure_stats_writer_started():
    global _stats_write_queue, _stats_writer_task
    if _stats_write_queue is None:
        _stats_write_queue = asyncio.Queue(maxsize=5000)
    if _stats_writer_task is None or _stats_writer_task.done():
        _stats_writer_task = asyncio.create_task(_stats_writer_loop())
async def _stats_writer_loop():
    while True:
        item = await _stats_write_queue.get()
        batch = [item]
        deadline = asyncio.get_running_loop().time() + _STATS_BATCH_WAIT_SECONDS
        while len(batch) < _STATS_BATCH_MAX:
            now = asyncio.get_running_loop().time()
            if now >= deadline:
                break
            try:
                batch.append(_stats_write_queue.get_nowait())
            except asyncio.QueueEmpty:
                await asyncio.sleep(0)
                break
        conn = _get_conn()
        now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        async with _write_lock:
            await conn.executemany(
                "INSERT INTO bot_stats (event_type, detail, created_at) VALUES (?,?,?)",
                [(ev, det, now_iso) for ev, det in batch],
            )
            await conn.commit()

def _get_conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("database.init_db() must be called before using the database")
    return _conn
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

    CREATE TABLE IF NOT EXISTS bot_chats (
        id          INTEGER PRIMARY KEY,
        title       TEXT,
        chat_type   TEXT NOT NULL,
        added_at    TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS bot_stats (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type  TEXT NOT NULL,
        detail      TEXT,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_stats_type ON bot_stats(event_type);
    CREATE INDEX IF NOT EXISTS idx_stats_created ON bot_stats(created_at);
    """)
    await _conn.commit()
    try:
        await _conn.execute("ALTER TABLE messages ADD COLUMN sender_id INTEGER")
        await _conn.commit()
        log.info("🔧 Миграция: добавлена колонка sender_id")
    except Exception:
        pass
    _ensure_msg_writer_started()
    _ensure_stats_writer_started()
    log.info("✅ DB инициализирована")
async def close_db():
    global _conn
    global _msg_writer_task, _msg_write_queue
    global _stats_writer_task, _stats_write_queue
    if _msg_writer_task is not None:
        _msg_writer_task.cancel()
        try:
            await _msg_writer_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        _msg_writer_task = None
    _msg_write_queue = None
    if _stats_writer_task is not None:
        _stats_writer_task.cancel()
        try:
            await _stats_writer_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        _stats_writer_task = None
    _stats_write_queue = None
    if _conn is not None:
        await _conn.close()
        _conn = None
        log.info("🔒 DB соединение закрыто")
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
    db = _get_conn()
    async with db.execute(
        "SELECT id, username, full_name, referrer_id, joined FROM users "
        "ORDER BY joined DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]
FREE_CACHE_LIMIT = 100_000  # лимиты архива убраны — практически безлимит для всех

async def _get_owner_limit(owner_id: int) -> int:
    # Архив безлимитен для всех — лимит постоянный, кэшировать нечего.
    return FREE_CACHE_LIMIT

async def _msg_writer_loop():
    while True:
        item = await _msg_write_queue.get()
        batch = [item]
        deadline = asyncio.get_running_loop().time() + _MSG_BATCH_WAIT_SECONDS
        while len(batch) < _MSG_BATCH_MAX:
            now = asyncio.get_running_loop().time()
            if now >= deadline:
                break
            try:
                batch.append(_msg_write_queue.get_nowait())
            except asyncio.QueueEmpty:
                await asyncio.sleep(0)
                break
        db = _get_conn()
        async with _write_lock:
            for owner_id, msg in batch:
                limit = await _get_owner_limit(owner_id)
                now_iso = datetime.now().isoformat()
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
                    msg["media_type"], msg["file_id"], now_iso
                ))
                c = _owner_cleanup_counter.get(owner_id, 0) + 1
                if c >= _OWNER_CLEANUP_EVERY:
                    await db.execute("""
                        DELETE FROM messages
                        WHERE owner_id=? AND id NOT IN (
                            SELECT id FROM messages WHERE owner_id=?
                            ORDER BY id DESC LIMIT ?
                        )
                    """, (owner_id, owner_id, limit))
                    c = 0
                _owner_cleanup_counter[owner_id] = c
            await db.commit()

async def save_message(owner_id: int, msg: dict):
    _get_conn()
    _ensure_msg_writer_started()
    await _msg_write_queue.put((owner_id, msg))
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
async def count_messages_by_sender(owner_id: int, sender_id: int) -> int:
    """Сколько сообщений конкретного собеседника лежит в архиве владельца."""
    db = _get_conn()
    async with db.execute(
        "SELECT COUNT(*) FROM messages WHERE owner_id=? AND sender_id=?",
        (owner_id, sender_id),
    ) as cur:
        return (await cur.fetchone())[0]
async def search_messages(owner_id: int, query: str) -> list[dict]:
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
async def save_intercepted(owner_id: int, data: dict) -> int:
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
async def add_bot_chat(chat_id: int, title: str, chat_type: str):
    conn = _get_conn()
    now = datetime.now().isoformat()
    async with _write_lock:
        await conn.execute("""
            INSERT INTO bot_chats (id, title, chat_type, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title     = excluded.title,
                chat_type = excluded.chat_type
        """, (chat_id, title or "", chat_type, now))
        await conn.commit()
async def remove_bot_chat(chat_id: int):
    conn = _get_conn()
    async with _write_lock:
        await conn.execute("DELETE FROM bot_chats WHERE id=?", (chat_id,))
        await conn.commit()
async def get_all_bot_chats() -> list[dict]:
    conn = _get_conn()
    async with conn.execute(
        "SELECT * FROM bot_chats ORDER BY added_at DESC"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]

async def purge_expired_saved():
    conn = _get_conn()
    now = datetime.now().isoformat()
    async with _write_lock:
        await conn.execute("DELETE FROM saved_messages WHERE expires_at <= ?", (now,))
        await conn.commit()
async def record_stat(event_type: str, detail: str = ""):
    """Асинхронно записывает событие в bot_stats (через батчер)."""
    _get_conn()
    _ensure_stats_writer_started()
    await _stats_write_queue.put((event_type, detail))
async def count_stats(event_type: str, since_iso: Optional[str] = None) -> int:
    """Кол-во событий типа event_type; since_iso — ISO-строка начала периода (включительно)."""
    conn = _get_conn()
    if since_iso is None:
        async with conn.execute(
            "SELECT COUNT(*) FROM bot_stats WHERE event_type=?", (event_type,)
        ) as cur:
            return (await cur.fetchone())[0]
    async with conn.execute(
        "SELECT COUNT(*) FROM bot_stats WHERE event_type=? AND created_at >= ?",
        (event_type, since_iso),
    ) as cur:
        return (await cur.fetchone())[0]

async def delete_stats_like(pattern: str) -> int:
    """Удаляет события bot_stats по LIKE-маске (например 'groq_key%_fail').

    Возвращает количество удалённых записей. Нужно для сброса статистики
    ошибок API-ключей в админке.
    """
    conn = _get_conn()
    async with _write_lock:
        cur = await conn.execute(
            "DELETE FROM bot_stats WHERE event_type LIKE ?", (pattern,)
        )
        await conn.commit()
        return cur.rowcount



