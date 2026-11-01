import pytest

from publisher import crypto
from publisher.accounts import AccountStore, PendingAuth
from publisher.connectors.mock import MockConnector
from publisher.models import AuthError, TokenSet
from tests.publisher.conftest import connect_mock


def test_tokens_encrypted_at_rest_and_key_generated(data_dir):
    store = AccountStore(data_dir)
    acct = connect_mock(store, "alice")
    raw = (data_dir / "accounts" / f"{acct['id']}.json").read_text()
    assert "at-alice" not in raw and "rt-alice" not in raw
    assert "tokens" not in acct                        # public view never carries tokens
    assert (data_dir / "secret.key").is_file()
    assert (data_dir / ".gitignore").read_text().strip().endswith("*")
    assert store.account(acct["id"]).tokens.refresh_token == "rt-alice"


def test_env_secret_key_used(data_dir, monkeypatch):
    monkeypatch.setenv("CLIPPER_SECRET_KEY", "a passphrase, hashed into a Fernet key")
    store = AccountStore(data_dir)
    acct = connect_mock(store, "bob")
    assert not (data_dir / "secret.key").exists()
    monkeypatch.setenv("CLIPPER_SECRET_KEY", "a different key")
    with pytest.raises(crypto.SecretError):
        AccountStore(data_dir).account(acct["id"])


def test_many_accounts_per_platform_and_reconnect_updates(data_dir):
    store = AccountStore(data_dir)
    a = connect_mock(store, "one")
    b = connect_mock(store, "two")
    again = connect_mock(store, "one")
    assert a["id"] != b["id"] and again["id"] == a["id"]
    assert [x["username"] for x in store.list("mock")] == ["one", "two"]
    assert len(AccountStore(data_dir).list()) == 2      # reloads from disk


def test_refresh_before_expiry(data_dir, expired_tokens):
    store = AccountStore(data_dir)
    acct = connect_mock(store, "carol")
    rec = store._accounts[acct["id"]]
    store._set_tokens(rec, expired_tokens)
    fresh = store.fresh(acct["id"], MockConnector())
    assert fresh.tokens.access_token != "old"
    assert store.account(acct["id"]).tokens.access_token == fresh.tokens.access_token


def test_failed_refresh_flags_reconnect(data_dir):
    store = AccountStore(data_dir)
    acct = connect_mock(store, "dave")
    store._set_tokens(store._accounts[acct["id"]],
                      TokenSet(access_token="old", refresh_token="revoked", expires_at=TokenSet.expiry(5)))
    with pytest.raises(AuthError):
        store.fresh(acct["id"], MockConnector())
    assert store.get_public(acct["id"])["status"] == "reconnect_needed"
    with pytest.raises(AuthError):          # stays blocked until the user reconnects
        store.fresh(acct["id"], MockConnector())
    connect_mock(store, "dave")
    assert store.get_public(acct["id"])["status"] == "connected"


def test_pending_state_single_use_and_platform_bound(data_dir):
    p = PendingAuth(data_dir)
    s = p.create("youtube", "verifier")
    assert p.pop(s, "tiktok") is None       # wrong platform burns it too
    s = p.create("youtube", "verifier")
    assert p.pop(s, "youtube")["verifier"] == "verifier"
    assert p.pop(s, "youtube") is None
