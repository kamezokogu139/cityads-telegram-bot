import aiosqlite
from config import DB_PATH, FERNET


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
