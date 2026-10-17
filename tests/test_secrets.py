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


def test_expired_token_is_never_sent_to_dhan():
    import base64, json, time
    from sandbox.market.dhan_feed import token_expired
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    jwt = lambda exp: f"{enc({'alg': 'HS512'})}.{enc({'exp': exp})}.sig"
    assert token_expired(jwt(time.time() - 10)) and not token_expired(jwt(time.time() + 3600))
    assert not token_expired("not-a-jwt")             # unknown shape: let Dhan decide
