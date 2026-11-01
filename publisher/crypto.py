"""Token encryption at rest (Fernet).

Key: env CLIPPER_SECRET_KEY (a Fernet key, or any passphrase, which is hashed into one). If unset,
a key is generated on first run into <data_dir>/secret.key; that folder gets its own `.gitignore`
so neither the key nor the encrypted tokens can be committed by accident.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_lock = threading.Lock()
_cache: dict[str, Fernet] = {}


class SecretError(RuntimeError):
    pass


def protect_dir(d: Path) -> None:
    """Create `d` with a `.gitignore` that ignores everything in it."""
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("# publisher data: OAuth tokens, secret key, job state. Never commit.\n*\n", encoding="utf-8")


def _key_from_env(value: str) -> bytes:
    raw = value.strip().encode()
    try:
        if len(base64.urlsafe_b64decode(raw)) == 32:
            return raw
    except (ValueError, base64.binascii.Error):
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


def fernet(data_dir: Path) -> Fernet:
    env = os.environ.get("CLIPPER_SECRET_KEY")
    cache_key = f"env:{env}" if env else f"file:{data_dir}"
    with _lock:
        if cache_key in _cache:
            return _cache[cache_key]
        if env:
            key = _key_from_env(env)
        else:
            protect_dir(data_dir)
            path = data_dir / "secret.key"
            if path.is_file():
                key = path.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                path.write_bytes(key)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        f = _cache[cache_key] = Fernet(key)
        return f


def encrypt(data_dir: Path, obj: dict) -> str:
    return fernet(data_dir).encrypt(json.dumps(obj).encode()).decode()


def decrypt(data_dir: Path, token: str) -> dict:
    try:
        return json.loads(fernet(data_dir).decrypt(token.encode()))
    except InvalidToken:
        raise SecretError("Stored tokens cannot be decrypted (CLIPPER_SECRET_KEY changed?). Reconnect the account.")
