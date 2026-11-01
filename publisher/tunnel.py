"""Free public https link to this server, for platforms that need one (Instagram, TikTok web apps).

Providers (all free):
  localhost.run  `ssh -R 80:localhost:<port> nokey@localhost.run`. Needs only the ssh client that
                 ships with Windows/macOS/Linux: no download, no account. The URL changes each
                 time the tunnel starts.
  cloudflared    `cloudflared tunnel --url http://localhost:<port>` if cloudflared is on PATH.
  ngrok          `ngrok http <port>` if ngrok is on PATH and has an authtoken; set NGROK_DOMAIN to
                 use your free static domain (the redirect URI then never changes).

Only the user starts a tunnel (button on the Accounts page, or POST /api/publish/tunnel/start).
While it runs, `install_tunnel_guard` (publisher/api.py) only lets the OAuth callbacks and the
temporary clip links through it; everything else is refused for requests arriving from outside.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from clipper.util import read_json, write_json
from publisher import config

URL_RE = re.compile(r"https://(?!admin\.)[a-z0-9][a-z0-9-]*\.(?:lhr\.life|localhost\.run|trycloudflare\.com|"
                    r"ngrok-free\.app|ngrok-free\.dev|ngrok\.app|ngrok\.io|ngrok\.dev)\b")
START_TIMEOUT_SEC = 40


def available_providers() -> list[str]:
    out = []
    if shutil.which("ssh"):
        out.append("localhost.run")
    if shutil.which("cloudflared"):
        out.append("cloudflared")
    if shutil.which("ngrok"):
        out.append("ngrok")
    return out


def command(provider: str, port: int) -> list[str]:
    if provider == "localhost.run":
        return ["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
                "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
                "-R", f"80:127.0.0.1:{port}", "nokey@localhost.run"]
    if provider == "cloudflared":
        return ["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
    if provider == "ngrok":
        cmd = ["ngrok", "http", str(port), "--log", "stdout", "--log-format", "logfmt"]
        if os.environ.get("NGROK_DOMAIN"):
            cmd[2:2] = [f"--url={os.environ['NGROK_DOMAIN']}"]
        return cmd
    raise ValueError(f"Unknown tunnel provider {provider!r}")


class Tunnel:
    def __init__(self, data_dir: Path, on_url=None):
        self.settings_path = data_dir / "tunnel.json"
        self.on_url = on_url or (lambda url: None)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._log: deque[str] = deque(maxlen=40)
        self.state = "off"          # off | starting | on | error
        self.url: str | None = None
        self.provider: str | None = None
        self.error: str | None = None
        self.port: int | None = None
        self.started_at: float | None = None
        self.command_override: list[str] | None = None   # tests

    # -- settings: remember to bring the link back after a restart ---------------------------

    def settings(self) -> dict:
        return read_json(self.settings_path) if self.settings_path.is_file() else {}

    def _save_settings(self, **kw) -> None:
        write_json(self.settings_path, {**self.settings(), **kw})

    def status(self) -> dict:
        return {"state": self.state, "url": self.url, "provider": self.provider, "error": self.error,
                "port": self.port, "providers": available_providers(),
                "autostart": bool(self.settings().get("autostart")),
                "env_url": os.environ.get("PUBLISHER_PUBLIC_BASE_URL") or None,
                "log": list(self._log)[-12:]}

    # -- lifecycle ------------------------------------------------------------------------

    def start(self, port: int, provider: str | None = None, remember: bool = True) -> dict:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return self.status()
            provider = provider or (self.settings().get("provider")) or next(iter(available_providers()), None)
            if not provider:
                raise RuntimeError("No tunnel tool found: install OpenSSH (ships with Windows 10+) or cloudflared.")
            cmd = self.command_override or command(provider, port)
            self._log.clear()
            self.state, self.url, self.error = "starting", None, None
            self.provider, self.port, self.started_at = provider, port, time.time()
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL, text=True, errors="replace",
                                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as e:
                self.state, self.error = "error", f"Could not start {provider}: {e}"
                return self.status()
            if remember:
                self._save_settings(autostart=True, provider=provider)
            threading.Thread(target=self._read, args=(self._proc,), name="tunnel-reader", daemon=True).start()
            threading.Thread(target=self._watchdog, args=(self._proc,), daemon=True).start()
        return self.status()

    def stop(self, forget: bool = True) -> dict:
        with self._lock:
            proc, self._proc = self._proc, None
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self.state, self.url, self.error = "off", None, None
            config.set_public_base(None)
            self.on_url(None)
            if forget:
                self._save_settings(autostart=False)
        return self.status()

    def wait_ready(self, timeout: float = START_TIMEOUT_SEC) -> bool:
        end = time.time() + timeout
        while time.time() < end and self.state == "starting":
            time.sleep(0.1)
        return self.state == "on"

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            self._log.append(line[:300])
            m = URL_RE.search(line)
            if m and proc is self._proc and self.url != m.group(0):
                self.url = m.group(0)
                self.state = "on"
                config.set_public_base(self.url)
                self.on_url(self.url)
        code = proc.wait()
        if proc is self._proc:
            self.state = "error"
            self.error = f"Tunnel exited (code {code}). " + (self._log[-1] if self._log else "")
            self.url = None
            config.set_public_base(None)
            self.on_url(None)

    def _watchdog(self, proc: subprocess.Popen) -> None:
        time.sleep(START_TIMEOUT_SEC)
        if proc is self._proc and self.state == "starting":
            why = "No public URL after 40 s. " + (self._log[-1] if self._log else "")
            self.stop(forget=False)
            self.state, self.error = "error", why
