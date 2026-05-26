import json
import logging
from datetime import datetime, time
from pathlib import Path

try:
    from .config import Config
except ImportError:
    from config import Config

logger = logging.getLogger(__name__)

TOKEN_FILE = Config.UPSTOX_TOKEN_FILE


def save_upstox_token(token: str) -> None:
    """Persist Upstox access token with a timestamp."""
    try:
        Path(TOKEN_FILE).parent.mkdir(parents=True, exist_ok=True)
        data = {
            "access_token": token,
            "fetched_at": datetime.now().isoformat(),
        }
        with open(TOKEN_FILE, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("Upstox access token saved to %s", TOKEN_FILE)
    except Exception as e:
        logger.error("Failed to save Upstox token: %s", e)


def load_upstox_token() -> str:
    """Return a valid Upstox access token, refreshing if stale (fetched before today 3AM)."""
    from upstox_login import get_upstox_access_token

    token_path = Path(TOKEN_FILE)

    if not token_path.exists():
        logger.info("No Upstox token file found. Generating new token.")
        token = get_upstox_access_token()
        save_upstox_token(token)
        return token

    try:
        with open(token_path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Upstox token file corrupted. Regenerating.")
        token = get_upstox_access_token()
        save_upstox_token(token)
        return token

    fetched_at_str = data.get("fetched_at")
    access_token = data.get("access_token")

    if not fetched_at_str or not access_token:
        logger.warning("Upstox token data incomplete. Regenerating.")
        token = get_upstox_access_token()
        save_upstox_token(token)
        return token

    fetched_at = datetime.fromisoformat(fetched_at_str)
    today_3am = datetime.combine(datetime.today(), time(3, 0))

    if fetched_at < today_3am:
        logger.info("Upstox token is from before today 3AM. Regenerating.")
        token = get_upstox_access_token()
        save_upstox_token(token)
        return token

    logger.info("Reusing existing Upstox token.")
    return access_token
