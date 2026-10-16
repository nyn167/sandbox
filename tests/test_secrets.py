"""Dhan credentials: shared secrets file first, the trader's .env as fallback (offline)."""
import pytest

from sandbox.config import CredentialError, dhan_credentials


def test_shared_secrets_file_wins_then_falls_back(tmp_path, monkeypatch):
    trader_env = tmp_path / "trader.env"
    trader_env.write_text("DHAN_CLIENT_ID=1\nDHAN_ACCESS_TOKEN=old\n", encoding="utf-8")
    shared = tmp_path / "secrets.env"
    monkeypatch.setenv("NYN_SECRETS_FILE", str(shared))
    cfg = {"market": {"dhan_env_file": str(trader_env)}}
    assert dhan_credentials(cfg) == ("1", "old")
    shared.write_text("DHAN_CLIENT_ID=1\nDHAN_ACCESS_TOKEN=new\n", encoding="utf-8")
    assert dhan_credentials(cfg) == ("1", "new")
    shared.write_text("DHAN_CLIENT_ID=1\nDHAN_ACCESS_TOKEN=trader\nDHAN2_CLIENT_ID=2\nDHAN2_ACCESS_TOKEN=data\n", encoding="utf-8")
    assert dhan_credentials(cfg) == ("2", "data")                  # never the trader's account when a second exists
    shared.write_text("KITE_API_KEY=k\n", encoding="utf-8")
    with pytest.raises(CredentialError, match="secrets.env"):
        dhan_credentials(cfg)
