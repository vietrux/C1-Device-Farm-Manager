"""Chạy df_agent.py / df-host.sh trên box qua SSH.

Script được đẩy qua stdin (dòng đầu là mật khẩu cho `sudo -S` nếu có), nên box
không cần cài gì. Kết nối SSH được dùng lại (ControlMaster) để ảnh màn hình và
thao tác chạm phản hồi nhanh.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional, Sequence

from .store import Box

log = logging.getLogger(__name__)

# kho image trên laptop (farm-image.sh pull ghi vào đây) + resetprop đã rút
IMAGE_CACHE = Path(os.environ.get("DF_IMAGE_CACHE",
                                  os.path.expanduser("~/.cache/devicefarm/images")))
RESETPROP_SRC = Path(os.environ.get("DF_RESETPROP",
                                    os.path.expanduser("~/.cache/devicefarm/integrity/magisk/resetprop")))


class RemoteError(RuntimeError):
    pass


class Remote:
    def __init__(self, scripts_dir: Path, control_dir: Path, connect_timeout: int = 10) -> None:
        self.scripts_dir = scripts_dir
        self.control_dir = control_dir
        self.connect_timeout = connect_timeout
        control_dir.mkdir(parents=True, exist_ok=True)
        control_dir.chmod(0o700)

    @property
    def agent_path(self) -> Path:
        return self.scripts_dir / "df_agent.py"

    @property
    def host_path(self) -> Path:
        return self.scripts_dir / "df-host.sh"

    def ssh_argv(self, box: Box, remote_cmd: str) -> List[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout}",
                "-o", "ServerAliveInterval=15", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ControlMaster=auto", "-o", f"ControlPath={self.control_dir}/%C",
                "-o", "ControlPersist=600", box.ssh, remote_cmd]

    @staticmethod
    def sudo_cmd(box: Box, inner: str) -> str:
        # có mật khẩu: sudo -S đọc dòng đầu stdin; không có: giả định NOPASSWD (-n)
        return f"sudo -S -p '' {inner}" if box.sudo_password else f"sudo -n {inner}"

    @staticmethod
    def stdin_for(box: Box, script: bytes) -> bytes:
        return (box.sudo_password.encode() + b"\n" + script) if box.sudo_password else script

    async def _exec(self, argv: Sequence[str], stdin: bytes, timeout: float) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RemoteError(f"quá {int(timeout)}s không phản hồi")
        return proc.returncode or 0, out, err

    async def agent(self, box: Box, *args: str, timeout: float = 60, raw: bool = False) -> Any:
        """Chạy 1 lệnh df_agent.py; trả JSON đã parse (hoặc chuỗi khi raw=True)."""
        script = self.agent_path.read_bytes()
        cmd = self.sudo_cmd(box, "python3 - " + shlex.join(args))
        rc, out, err = await self._exec(self.ssh_argv(box, cmd), self.stdin_for(box, script), timeout)
        text = out.decode(errors="replace").strip()
        if rc == 255:
            raise RemoteError(f"không SSH được tới {box.ssh}: {err.decode(errors='replace').strip()[-300:]}")
        if raw and rc == 0:
            return text
        try:
            data = json.loads(text.splitlines()[-1]) if text else None
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "error" in data:
            raise RemoteError(data["error"])
        if rc != 0 or data is None:
            msg = err.decode(errors="replace").strip() or text
            if "a password is required" in msg or "incorrect password" in msg:
                msg = "sudo cần mật khẩu (nhập mật khẩu sudo cho box này)"
            raise RemoteError(msg[-400:] or f"agent exit {rc}")
        return data

    async def host_script(self, box: Box, args: Sequence[str],
                          on_line: Callable[[str], Awaitable[None] | None], timeout: float = 600) -> int:
        """Chạy df-host.sh, đẩy từng dòng output ra on_line; trả exit code."""
        script = self.host_path.read_bytes()
        cmd = self.sudo_cmd(box, "sh -s -- " + shlex.join(args))
        proc = await asyncio.create_subprocess_exec(
            *self.ssh_argv(box, cmd), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        assert proc.stdin and proc.stdout
        proc.stdin.write(self.stdin_for(box, script))
        await proc.stdin.drain()
        proc.stdin.close()

        async def pump() -> None:
            assert proc.stdout
            async for line in proc.stdout:
                r = on_line(line.decode(errors="replace").rstrip("\n"))
                if asyncio.iscoroutine(r):
                    await r
        try:
            await asyncio.wait_for(pump(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RemoteError(f"df-host.sh quá {int(timeout)}s")
        return await proc.wait()

    async def install_resetprop(self, box: Box, on_line: Callable[[str], Any]) -> bool:
        """Đảm bảo /opt/df/resetprop trên box — df_agent cần nó để áp 77 props
        hồ sơ + IMEI lúc máy vừa boot; thiếu là máy mới lộ fingerprint rockchip
        thô (Google chặn). Trả True nếu đã có sẵn, False nếu vừa cài."""
        rc, _, _ = await self._exec(
            self.ssh_argv(box, self.sudo_cmd(box, "test -x /opt/df/resetprop")),
            self.stdin_for(box, b""), 30)
        if rc == 0:
            on_line("resetprop: đã có sẵn trên box")
            return True
        if not RESETPROP_SRC.exists():
            raise RemoteError(
                f"thiếu {RESETPROP_SRC} — chạy ./tools/integrity/fetch-assets.sh trước "
                "(máy mới sẽ KHÔNG có hồ sơ thiết bị nếu thiếu resetprop)")
        data = RESETPROP_SRC.read_bytes()
        cmd = self.sudo_cmd(
            box, "sh -c 'mkdir -p /opt/df && cat > /opt/df/resetprop && chmod 755 /opt/df/resetprop'")
        rc, _, err = await self._exec(self.ssh_argv(box, cmd), self.stdin_for(box, data), 60)
        if rc != 0:
            raise RemoteError(f"cài resetprop lỗi: {err.decode(errors='replace').strip()[-200:]}")
        on_line(f"resetprop: đã cài /opt/df/resetprop (md5 {hashlib.md5(data).hexdigest()[:8]})")
        return False

    async def ensure_image(self, box: Box, image: str,
                           on_line: Callable[[str], Any], timeout: float = 1800) -> bool:
        """Image chính đã có trên box chưa? Thiếu thì nạp từ kho laptop
        (~/.cache/devicefarm/images — farm-image.sh pull ghi vào) rồi đọc lại
        chain-md5 trên box để xác nhận "chain nguyên vén" chứ không chỉ "load xong".
        Trả True nếu đã có sẵn, False nếu vừa nạp."""
        info = await self.agent(box, "images", timeout=60)
        refs = {i.get("ref") for i in (info or {}).get("images", [])}
        if image in refs:
            on_line(f"image chính {image}: đã có sẵn trên box")
            return True
        mf = IMAGE_CACHE / "manifest.json"
        if not mf.exists():
            raise RemoteError(f"box thiếu image '{image}' và không thấy kho {mf} — "
                              "nạp tay: ./tools/box/farm-image.sh push <alias> user@box")
        ent = json.loads(mf.read_text()).get(image)
        if not ent:
            raise RemoteError(f"kho laptop không có '{image}' — chạy farm-image.sh pull trước")
        f = IMAGE_CACHE / ent["file"]
        if not f.exists():
            raise RemoteError(f"thiếu file kho {f.name}")
        on_line(f"verify sha256 {f.name} ({ent.get('size', 0) / 1e9:.2f} GB)…")
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(16 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != ent.get("sha256"):
            raise RemoteError(f"sha256 {f.name} không khớp manifest — kho hỏng?")
        on_line("sha256 ✓ — đẩy image vào box (docker load), chờ vài phút…")
        cmd = self.sudo_cmd(box, "docker load")
        proc = await asyncio.create_subprocess_exec(
            *self.ssh_argv(box, cmd), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        assert proc.stdin and proc.stdout
        pw = (box.sudo_password.encode() + b"\n") if box.sudo_password else b""

        async def feed() -> None:
            if pw:
                proc.stdin.write(pw)
                await proc.stdin.drain()
            with open(f, "rb") as fh:
                while True:
                    chunk = fh.read(16 << 20)
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
            proc.stdin.close()

        feeder = asyncio.create_task(feed())

        async def pump() -> None:
            async for line in proc.stdout:
                on_line(line.decode(errors="replace").rstrip())

        try:
            await asyncio.wait_for(pump(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RemoteError(f"docker load quá {int(timeout)}s")
        rc = await proc.wait()
        try:
            await feeder
        except Exception as e:   # noqa: BLE001
            raise RemoteError(f"stream image lỗi: {e}")
        if rc != 0:
            raise RemoteError(f"docker load exit {rc}")
        lib = await self.agent(box, "image-lib", image, timeout=180)
        md5 = (lib or {}).get("md5") or ""
        if ent.get("chain_md5") and md5 != ent["chain_md5"]:
            raise RemoteError(f"chain md5 sau load ({md5[:8]}) ≠ kho ({ent['chain_md5'][:8]})")
        on_line(f"image chính {image}: nạp xong, chain nguyên vén ✓ (md5 {md5[:8]})")
        return False

    async def write_file(self, box: Box, path: str, data: bytes, timeout: float = 120,
                          skip_if_size: Optional[int] = None) -> bool:
        """Ghi file nhị phân lên box qua cùng SSH master (chạy với user ssh, không sudo —
        path phải ghi được, vd /tmp/...). Trả False nếu bỏ qua vì bản cùng size đã có."""
        if skip_if_size is not None:
            rc, out, _ = await self._exec(
                self.ssh_argv(box, f"stat -c %s '{path}' 2>/dev/null || true"), b"", 15)
            if out.decode(errors="replace").strip() == str(skip_if_size):
                return False
        cmd = f"mkdir -p \"$(dirname '{path}')\" && cat > '{path}' && chmod 644 '{path}'"
        rc, _, err = await self._exec(self.ssh_argv(box, cmd), data, timeout)
        if rc != 0:
            raise RemoteError(f"không ghi được {path} lên {box.ssh}: "
                              f"{err.decode(errors='replace').strip()[-200:]}")
        return True

    async def forward(self, box: Box, lport: int, host: str, hport: int) -> None:
        """Thêm port forward 127.0.0.1:lport -> host:hport vào kết nối SSH master của box."""
        rc, _, err = await self._exec(self.ssh_argv(box, "true"), b"", self.connect_timeout + 5)
        if rc != 0:
            raise RemoteError(f"không SSH được tới {box.ssh}: {err.decode(errors='replace').strip()[-300:]}")
        argv = self.ssh_argv(box, "")[:-1]
        argv[1:1] = ["-O", "forward", "-L", f"127.0.0.1:{lport}:{host}:{hport}"]
        rc, _, err = await self._exec(argv, b"", self.connect_timeout + 5)
        msg = err.decode(errors="replace")
        if rc != 0 and "already" not in msg and "File exists" not in msg:
            raise RemoteError(f"không mở được forward tới {host}:{hport}: {msg.strip()[-300:]}")

    async def reset_master(self, box: Box) -> None:
        """Đóng kết nối SSH master của box (bỏ mọi port forward cũ còn treo trên đó)."""
        argv = self.ssh_argv(box, "")[:-1]
        argv[1:1] = ["-O", "exit"]
        await self._exec(argv, b"", self.connect_timeout + 5)

    async def check_ssh(self, box: Box) -> Optional[str]:
        """None nếu SSH bằng key được, ngược lại là thông báo lỗi."""
        rc, _, err = await self._exec(self.ssh_argv(box, "true"), b"", self.connect_timeout + 5)
        return None if rc == 0 else (err.decode(errors="replace").strip()[-300:] or f"ssh exit {rc}")
