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


def dhan_credentials(cfg: dict) -> tuple[str, str]:
    """Client id + access token, re-read each call from the trader's .env (read-only, only these two keys)."""
    env = dotenv_values(Path(cfg["market"]["dhan_env_file"]).expanduser())
    cid, tok = (env.get("DHAN_CLIENT_ID") or "").strip(), (env.get("DHAN_ACCESS_TOKEN") or "").strip()
    if not cid or not tok:
        raise CredentialError("No Dhan credentials in " + cfg["market"]["dhan_env_file"] + TOKEN_HELP)
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
