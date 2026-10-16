"""Config, paths and credentials. PAPER TRADING ONLY — nothing in this project places real orders."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TOKEN_HELP = "\n→ The Dhan token is shared with the NIFTY OI trader: paste a fresh one into the Telegram bot."


class CredentialError(RuntimeError):
    pass


def load_config(path: Path | None = None) -> dict:
    return yaml.safe_load(Path(path or ROOT / "config.yaml").read_text(encoding="utf-8"))


def secrets_path() -> Path:
    """The secrets file shared by every NYN app (the trader's Telegram bot keeps the daily Dhan tokens in it)."""
    default = r"C:\nyn\secrets.env" if os.name == "nt" else "~/nyn/secrets.env"
    return Path(os.getenv("NYN_SECRETS_FILE") or default).expanduser()


def dhan_credentials(cfg: dict) -> tuple[str, str]:
    """Client id + access token (account 2 when configured), re-read each call, read-only, only these two keys:
    from the shared secrets file when it exists, else from market.dhan_env_file (the trader's .env)."""
    src = secrets_path()
    if not src.is_file():
        src = Path(cfg["market"]["dhan_env_file"]).expanduser()
    env = dotenv_values(src)
    # Dhan rate limits are per account: the sandbox uses account 2 (DHAN2_*) so it never competes with the live
    # NIFTY trader on account 1. Account 1 only when no second account is configured at all.
    slot = "DHAN2" if (env.get("DHAN2_CLIENT_ID") or "").strip() else "DHAN"
    cid, tok = (env.get(f"{slot}_CLIENT_ID") or "").strip(), (env.get(f"{slot}_ACCESS_TOKEN") or "").strip()
    if not cid or not tok:
        raise CredentialError(f"No Dhan credentials in {src}" + TOKEN_HELP)
    return cid, tok


def admin_key() -> str:
    """Admin API key from sandbox .env; generated on first run (never logged)."""
    env_path = ROOT / ".env"
    env = dotenv_values(env_path) if env_path.exists() else {}
    key = (env.get("SANDBOX_ADMIN_KEY") or os.getenv("SANDBOX_ADMIN_KEY") or "").strip()
    if not key:
        key = "adm_" + secrets.token_urlsafe(24)
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(f"SANDBOX_ADMIN_KEY={key}\n")
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
    return key
