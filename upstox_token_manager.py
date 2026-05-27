import fcntl
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
_LOCK_FILE = Path(str(TOKEN_FILE) + ".lock")


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


def _is_token_fresh(data: dict) -> bool:
    """Return True if the token was fetched today after 3 AM."""
    fetched_at_str = data.get("fetched_at")
    access_token = data.get("access_token")
    if not fetched_at_str or not access_token:
        return False
    try:
        fetched_at = datetime.fromisoformat(fetched_at_str)
        today_3am = datetime.combine(datetime.today(), time(3, 0))
        return fetched_at >= today_3am
    except Exception:
        return False


def load_upstox_token() -> str:
    """
    Return a valid Upstox access token.

    Uses an exclusive file lock so that when multiple containers start
    simultaneously only one runs the Selenium login; the rest wait and
    then reuse the token written by the winner.
    """
    from upstox_login import get_upstox_access_token

    token_path = Path(TOKEN_FILE)

    # Fast path: valid token already on disk — no lock needed for a plain read.
    if token_path.exists():
        try:
            data = json.loads(token_path.read_text())
            if _is_token_fresh(data):
                logger.info("Reusing existing Upstox token.")
                return data["access_token"]
        except Exception:
            pass

    # Slow path: token missing or stale.  Acquire an exclusive lock so only
    # one process runs Selenium; all others block here and then re-read the
    # file once the winner releases the lock.
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOCK_FILE, "w") as lf:
        logger.info("Waiting for Upstox token lock…")
        fcntl.flock(lf, fcntl.LOCK_EX)
        logger.info("Token lock acquired.")

        # Re-check: another process may have generated it while we waited.
        if token_path.exists():
            try:
                data = json.loads(token_path.read_text())
                if _is_token_fresh(data):
                    logger.info("Reusing token written by peer process.")
                    return data["access_token"]
            except Exception:
                pass

        logger.info("Generating new Upstox access token via Selenium.")
        token = get_upstox_access_token()
        save_upstox_token(token)
        return token
