import os
from dotenv import load_dotenv
from cryptography.fernet import Fernet

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.path.join(os.path.dirname(__file__), "cityads.db")

_key = os.getenv("ENCRYPTION_KEY", "")
if not _key:
    _key = Fernet.generate_key().decode()
    print(f"⚠️  ENCRYPTION_KEY not set. Generated new key: {_key}")
    print("   Add it to .env, otherwise data will be unreadable after restart.")

FERNET = Fernet(_key.encode() if isinstance(_key, str) else _key)

CITYADS_AUTH_URL = "https://auth2.cityads.com/oauth/access_token"
CITYADS_API_BASE = "https://cityads.com/api/rest/webmaster"

# Seedance 2.0 API — https://seedance2.ai/api-docs
SEEDANCE_API_KEY = os.getenv("SEEDANCE_API_KEY", "").strip()
SEEDANCE_API_BASE = os.getenv("SEEDANCE_API_BASE", "https://api.seedance2.ai").rstrip("/")
SEEDANCE_MONTHLY_CREDITS = int(os.getenv("SEEDANCE_MONTHLY_CREDITS", "1600"))

# S3-compatible storage for public media URLs (image/reference modes)
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "").strip()
S3_BUCKET = os.getenv("S3_BUCKET", "").strip()
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "").strip()
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "").strip()
S3_PUBLIC_BASE_URL = os.getenv("S3_PUBLIC_BASE_URL", "").strip()


def seedance_configured() -> bool:
    return bool(SEEDANCE_API_KEY)


def media_upload_configured() -> bool:
    return bool(S3_BUCKET and S3_ACCESS_KEY and S3_SECRET_KEY and S3_PUBLIC_BASE_URL)
