import aiosqlite
import time
from datetime import datetime, timezone

from config import DB_PATH, FERNET, SEEDANCE_MONTHLY_CREDITS


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id      INTEGER PRIMARY KEY,
                client_id        TEXT NOT NULL,
                client_secret    TEXT NOT NULL,
                access_token     TEXT,
                token_expires_at REAL DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id        TEXT PRIMARY KEY,
                status        TEXT NOT NULL,
                mode_id       TEXT NOT NULL,
                prompt        TEXT NOT NULL,
                image_url     TEXT,
                width         INTEGER,
                height        INTEGER,
                aspect_ratio  TEXT,
                megapixels    REAL,
                result_url    TEXT,
                error         TEXT,
                chat_id       INTEGER NOT NULL,
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL,
                locked_until  REAL NOT NULL DEFAULT 0
            )
        """)
        await _ensure_jobs_columns(conn)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_kv (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await conn.commit()


async def save_credentials(telegram_id: int, client_id: str, client_secret: str):
    enc_id = FERNET.encrypt(client_id.encode()).decode()
    enc_secret = FERNET.encrypt(client_secret.encode()).decode()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, client_id, client_secret)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                client_id = excluded.client_id,
                client_secret = excluded.client_secret,
                access_token = NULL, token_expires_at = 0
            """,
            (telegram_id, enc_id, enc_secret),
        )
        await conn.commit()


async def get_credentials(telegram_id: int) -> tuple[str, str] | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT client_id, client_secret FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if not row or not row["client_id"]:
            return None
        return (
            FERNET.decrypt(row["client_id"].encode()).decode(),
            FERNET.decrypt(row["client_secret"].encode()).decode(),
        )


async def get_cached_token(telegram_id: int) -> tuple[str | None, float]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT access_token, token_expires_at FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if not row or not row["access_token"]:
            return None, 0
        return FERNET.decrypt(row["access_token"].encode()).decode(), row["token_expires_at"]


async def save_token(telegram_id: int, access_token: str, expires_at: float):
    enc_token = FERNET.encrypt(access_token.encode()).decode()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE users SET access_token = ?, token_expires_at = ? WHERE telegram_id = ?",
            (enc_token, expires_at, telegram_id),
        )
        await conn.commit()


async def delete_user(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        await conn.commit()


async def is_connected(telegram_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        return await cursor.fetchone() is not None


async def get_kv(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute("SELECT value FROM bot_kv WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_kv(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO bot_kv (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await conn.commit()


SEEDANCE_CREDITS_KEY = "seedance_credits_available"
SEEDANCE_USER_CREDITS_PREFIX = "seedance_user_credits:"
SEEDANCE_USER_PERIOD_PREFIX = "seedance_user_period:"


def _current_credit_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def get_user_credits(telegram_id: int) -> int:
    """Return user's remaining credits for the current month (auto-reset)."""
    period_key = f"{SEEDANCE_USER_PERIOD_PREFIX}{telegram_id}"
    credits_key = f"{SEEDANCE_USER_CREDITS_PREFIX}{telegram_id}"
    current_period = _current_credit_period()
    stored_period = await get_kv(period_key)

    if stored_period != current_period:
        await set_kv(period_key, current_period)
        await set_kv(credits_key, str(SEEDANCE_MONTHLY_CREDITS))
        return SEEDANCE_MONTHLY_CREDITS

    raw = await get_kv(credits_key)
    if raw is None:
        await set_kv(period_key, current_period)
        await set_kv(credits_key, str(SEEDANCE_MONTHLY_CREDITS))
        return SEEDANCE_MONTHLY_CREDITS
    try:
        return int(raw)
    except ValueError:
        await set_kv(credits_key, str(SEEDANCE_MONTHLY_CREDITS))
        return SEEDANCE_MONTHLY_CREDITS


async def deduct_user_credits(telegram_id: int, amount: int) -> int:
    """Deduct credits after a successful generation; returns new balance."""
    current = await get_user_credits(telegram_id)
    updated = max(0, current - amount)
    await set_kv(f"{SEEDANCE_USER_CREDITS_PREFIX}{telegram_id}", str(updated))
    return updated


async def get_seedance_credits() -> int | None:
    raw = await get_kv(SEEDANCE_CREDITS_KEY)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def set_seedance_credits(amount: int):
    await set_kv(SEEDANCE_CREDITS_KEY, str(max(0, amount)))


async def adjust_seedance_credits(delta: int) -> int | None:
    current = await get_seedance_credits()
    if current is None:
        return None
    updated = max(0, current + delta)
    await set_seedance_credits(updated)
    return updated


async def _ensure_jobs_columns(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(jobs)")
    rows = await cursor.fetchall()
    cols = {row[1] for row in rows}
    if "width" not in cols:
        await conn.execute("ALTER TABLE jobs ADD COLUMN width INTEGER")
    if "height" not in cols:
        await conn.execute("ALTER TABLE jobs ADD COLUMN height INTEGER")
    if "aspect_ratio" not in cols:
        await conn.execute("ALTER TABLE jobs ADD COLUMN aspect_ratio TEXT")
    if "megapixels" not in cols:
        await conn.execute("ALTER TABLE jobs ADD COLUMN megapixels REAL")
    if "duration_sec" not in cols:
        await conn.execute("ALTER TABLE jobs ADD COLUMN duration_sec INTEGER")


async def create_job(
    *,
    job_id: str,
    status: str,
    mode_id: str,
    prompt: str,
    image_url: str | None,
    chat_id: int,
    aspect_ratio: str | None = None,
    megapixels: float | None = None,
    locked_until: float = 0,
    duration_sec: int | None = None,
) -> None:
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO jobs (
                job_id, status, mode_id, prompt, image_url,
                aspect_ratio, megapixels, result_url, error,
                chat_id, created_at, updated_at, locked_until, duration_sec
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                job_id, status, mode_id, prompt, image_url,
                aspect_ratio, megapixels,
                chat_id, now, now, locked_until, duration_sec,
            ),
        )
        await conn.commit()


async def claim_next_job(*, lock_seconds: int) -> dict | None:
    now = time.time()
    locked_until = now + lock_seconds
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        # SQLite: BEGIN IMMEDIATE makes selection+claim atomic.
        await conn.execute("BEGIN IMMEDIATE")
        row = await (await conn.execute(
            """
            SELECT job_id, status, mode_id, prompt, image_url, aspect_ratio, megapixels, duration_sec, chat_id
            FROM jobs
            WHERE status = 'queued'
               OR (status = 'running' AND locked_until < ?)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (now,),
        )).fetchone()

        if not row:
            await conn.execute("COMMIT")
            return None

        job_id = row["job_id"]
        await conn.execute(
            """
            UPDATE jobs
            SET status='running', updated_at=?, locked_until=?
            WHERE job_id=? AND (status='queued' OR locked_until < ?)
            """,
            (now, locked_until, job_id, now),
        )
        await conn.execute("COMMIT")
        return dict(row)


async def get_job(job_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute(
            """
            SELECT
                job_id, status, mode_id, prompt, image_url,
                aspect_ratio, megapixels, duration_sec, result_url, error,
                chat_id, created_at, updated_at, locked_until
            FROM jobs
            WHERE job_id = ?
            """,
            (job_id,),
        )).fetchone()
        return dict(row) if row else None


async def set_job_status(
    *,
    job_id: str,
    status: str,
    result_url: str | None = None,
    error: str | None = None,
) -> None:
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            UPDATE jobs
            SET
                status=?,
                result_url=?,
                error=?,
                updated_at=?,
                locked_until=0
            WHERE job_id=?
            """,
            (status, result_url, error, now, job_id),
        )
        await conn.commit()


async def delete_job(job_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
        await conn.commit()
