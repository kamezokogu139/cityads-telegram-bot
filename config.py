import os
import base64
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
