"""Per-container egress proxy — set/clear a SOCKS5 (or HTTP) upstream per device.

Why this lives next to integrity: passing Play Integrity is necessary but not
sufficient to log in to X / banks / TikTok. Those also score IP reputation and
"too many devices on one IP". For a farm, each container needs its own clean
egress IP. The dobox image already applies a per-container proxy to the whole
device (persist.s5 + tun2socks) via its ``/proxy`` extservice; we just drive it
through adbd, the same auditable push-and-run path as integrity-fix.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .adb import Adb
from .store import Box

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEVICE_DIR = REPO_ROOT / "tools" / "net" / "device"
REMOTE_TMP = "/data/local/tmp"
LogFn = Callable[[str], None]

TYPE_SOCKS5 = "2"
TYPE_HTTP = "1"
_TYPES = {"socks5": TYPE_SOCKS5, "http": TYPE_HTTP}


class ProxyError(Exception):
    pass


class Proxy:
    def __init__(self, adb: Adb, device_dir: Path = DEVICE_DIR) -> None:
        self.adb = adb
        self.device_dir = Path(device_dir)

    async def status(self, box: Box, ip: str) -> Dict[str, Any]:
        out = await self._run(box, ip, "proxy.sh", "status")
        return self._parse(out)

    async def set(self, box: Box, ip: str, *, host: str, port: int,
                  kind: str = "socks5", user: str = "", password: str = "",
                  log_fn: Optional[LogFn] = None) -> Dict[str, Any]:
        say = log_fn or (lambda _s: None)
        t = _TYPES.get(kind.lower())
        if t is None:
            raise ProxyError(f"unknown proxy type '{kind}' (socks5|http)")
        if not host or not port:
            raise ProxyError("host and port required")
        say(f"setting {kind} egress → {host}:{port}"
            + (f" (auth {user})" if user else ""))
        out = await self._run(box, ip, "proxy.sh", "set", t, host, str(port),
                              user, password)
        if "PROXY_FAIL" in out:
            raise ProxyError(_fail(out))
        return self._parse(out)

    async def clear(self, box: Box, ip: str, log_fn: Optional[LogFn] = None) -> Dict[str, Any]:
        say = log_fn or (lambda _s: None)
        say("clearing proxy → direct egress")
        out = await self._run(box, ip, "proxy.sh", "clear")
        if "PROXY_FAIL" in out:
            raise ProxyError(_fail(out))
        return self._parse(out)

    # -------------------------------------------------------------- helpers
    async def _run(self, box: Box, ip: str, script: str, *args: str) -> str:
        local = self.device_dir / script
        if not local.exists():
            raise ProxyError(f"device script missing: {local}")
        remote = f"{REMOTE_TMP}/{script}"
        await self.adb.push(box, ip, str(local), remote)
        return await self.adb.shell(box, ip, "sh", remote, *args, timeout=45)

    def _parse(self, out: str) -> Dict[str, Any]:
        line = next((l for l in out.splitlines() if l.strip().startswith("{")), "")
        try:
            return json.loads(line)
        except Exception:
            raise ProxyError(f"proxy.sh gave no JSON: {out.strip()[:200]}")


def _fail(out: str) -> str:
    return next((l for l in out.splitlines() if "PROXY_FAIL" in l), out.strip()[:200])
