"""Chạy df_agent.py / df-host.sh trên box qua SSH.

Script được đẩy qua stdin (dòng đầu là mật khẩu cho `sudo -S` nếu có), nên box
không cần cài gì. Kết nối SSH được dùng lại (ControlMaster) để ảnh màn hình và
thao tác chạm phản hồi nhanh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional, Sequence

from .store import Box

log = logging.getLogger(__name__)


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
