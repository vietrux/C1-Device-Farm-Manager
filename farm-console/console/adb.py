"""Màn hình + thao tác chạm qua adbd của máy Android.

Không dùng `docker exec screencap/input`: tiến trình docker exec mang context
SELinux `u:r:kernel:s0`, policy Android chặn nó gọi binder tới SurfaceFlinger /
InputManager nên lệnh treo. adbd chạy ở context `shell` hợp lệ.

Đường đi: console --(port forward trên kết nối SSH master)--> <ip máy>:5555.
Public key adb của máy chạy console đã được cài vào máy lúc tạo (adb_keys).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import zlib
from typing import Dict, List, Tuple

from .remote import Remote, RemoteError
from .store import Box

log = logging.getLogger(__name__)

PORT_BASE = 17000
PORT_SLOTS = 100  # mỗi box một dải 256 cổng: PORT_BASE + slot*256 + octet cuối của IP


class Adb:
    def __init__(self, remote: Remote, adb_bin: str = "adb",
                 key_pub: Optional[str] = None) -> None:
        self.remote = remote
        self.adb_bin = shutil.which(adb_bin) or adb_bin
        self.key_pub = (key_pub or "").strip() or None   # pubkey adb của console (cấp khi bị từ chối)
        self._ports: Dict[Tuple[str, str], int] = {}
        self._slots: Dict[str, int] = {}  # box.ssh -> slot, cố định theo thứ tự config
        self._verified: set[str] = set()  # serial đã xác minh đúng IP trong lần chạy này
        self._lock = asyncio.Lock()

    def refresh_slots(self, boxes) -> None:
        """Slot port là HÀM THUẦN của tập địa chỉ ssh trong config.

        Đây là điểm gỡ lỗi "tunnel trùng nhau khi ≥2 box": trước đây slot chỉ là
        crc32(box.ssh)%100 và xung đột được dịch +256 THEO LÚC CẤP PHÁT → sau khi
        console khởi động lại, thứ tự khác → port của hai box hoán đổi cho nhau →
        serial trỏ nhầm máy, reset_master dọn dồn chết cả cụm tunnel của box.
        Giờ: nhóm box theo slot crc32 mong muốn, trong nhóm trùng sắp theo ssh,
        dò slot rỗng kế tiếp — kết quả chỉ phụ thuộc TẬP ssh, không phụ thuộc
        thứ tự config hay thứ tự cấp phát. Restart/đảo thứ tự bao nhiêu lần
        cũng ra đúng cùng một phân bố slot.
        """
        live = sorted({b.ssh for b in boxes})
        if len(live) >= PORT_SLOTS:
            raise RemoteError(f"{len(live)} box không vừa {PORT_SLOTS} slot — tăng PORT_SLOTS")
        want: Dict[int, List[str]] = {}
        for ssh in live:
            want.setdefault(zlib.crc32(ssh.encode()) % PORT_SLOTS, []).append(ssh)
        taken: set[int] = set()
        out: Dict[str, int] = {}
        for slot, group in sorted(want.items()):
            for ssh in group:  # group đã theo thứ tự abc (live đã sorted)
                s = slot
                while s in taken:
                    s = (s + 1) % PORT_SLOTS
                taken.add(s)
                out[ssh] = s
        self._slots = out

    def _purge_verified(self, box: Box) -> None:
        """Bỏ trạng thái "đã verify" của các serial thuộc box (sau reset_master)."""
        ports = {str(p) for (ssh, _), p in self._ports.items() if ssh == box.ssh}
        self._verified = {s for s in self._verified if s.rsplit(":", 1)[-1] not in ports}

    @property
    def available(self) -> bool:
        return shutil.which(self.adb_bin) is not None

    async def _run(self, *args: str, timeout: float = 20) -> Tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            self.adb_bin, *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RemoteError(f"adb {args[0]} quá {int(timeout)}s")
        return proc.returncode or 0, out, err

    def port_for(self, box: Box, ip: str) -> int:
        """Cổng cục bộ CỐ ĐỊNH theo (box, IP).

        Port forward nằm trên kết nối SSH master, sống lâu hơn tiến trình console.
        Nếu cấp cổng theo thứ tự thì sau khi console khởi động lại, một cổng cũ còn
        trỏ tới máy khác -> hiển thị nhầm màn hình. Cổng cố định tránh việc đó;
        serial() vẫn xác minh lại IP thật của máy.
        """
        key = (box.ssh, ip)
        if key not in self._ports:
            if box.ssh not in self._slots:  # chưa refresh bao giờ: fallback 1 box
                self.refresh_slots([box])
            port = PORT_BASE + self._slots[box.ssh] * 256 + int(ip.rsplit(".", 1)[1])
            self._ports[key] = port
        return self._ports[key]

    async def _device_has_ip(self, serial: str, ip: str) -> bool:
        rc, out, _ = await self._run("-s", serial, "shell", "ip", "-4", "-o", "addr", timeout=10)
        return rc == 0 and f"inet {ip}/" in out.decode(errors="replace")

    async def serial(self, box: Box, ip: str, force: bool = False) -> str:
        """Đảm bảo có forward + adb connect; trả serial 127.0.0.1:<port>."""
        if not self.available:
            raise RemoteError("máy chạy console chưa có adb (apt install adb)")
        async with self._lock:
            port = self.port_for(box, ip)
            serial = f"127.0.0.1:{port}"
            if not force and serial in self._verified:
                rc, out, _ = await self._run("-s", serial, "get-state", timeout=5)
                if rc == 0 and out.strip() == b"device":
                    return serial
            for attempt in (0, 1):
                await self.remote.forward(box, port, ip, 5555)
                try:
                    await self._connect(serial, ip)
                except RemoteError as e:
                    if "chưa được máy cho phép" in str(e) and self.key_pub and attempt == 0:
                        # máy được tạo từ laptop khác (đồng nghiệp dùng chung farm):
                        # cấp key của MÌNH cho máy rồi connect lại — không cần tạo lại máy
                        log.info("%s chưa biết key của console — cấp key qua agent rồi thử lại", serial)
                        try:
                            await self.remote.agent(box, "adbkey-add", ip, self.key_pub, timeout=60)
                        except RemoteError as ae:
                            raise RemoteError(f"cấp adb key cho {ip} thất bại: {ae}")
                        continue
                    raise
                if await self._device_has_ip(serial, ip):
                    self._verified.add(serial)
                    return serial
                self._verified.discard(serial)
                # cổng đang trỏ nhầm máy (forward cũ còn trên SSH master): làm lại từ đầu
                log.warning("%s không phải máy %s, mở lại kết nối SSH tới %s", serial, ip, box.ssh)
                await self._run("disconnect", serial, timeout=5)
                await self.remote.reset_master(box)
                self._purge_verified(box)  # tunnel của cả box này giờ đã sạch
            raise RemoteError(f"cổng {port} không trỏ tới máy {ip}; thử lại sau ít phút")

    async def _connect(self, serial: str, ip: str) -> None:
        await self._run("disconnect", serial, timeout=5)
        rc, out, err = await self._run("connect", serial, timeout=10)
        text = (out + err).decode(errors="replace").strip()
        if "connected" not in text or "failed" in text or "unable" in text:
            raise RemoteError(f"adb connect {ip} thất bại: {text}")
        for _ in range(10):
            rc, out, _ = await self._run("-s", serial, "get-state", timeout=5)
            if rc == 0 and out.strip() == b"device":
                return
            await asyncio.sleep(0.5)
        raise RemoteError("adb chưa được máy cho phép (key adb của console chưa có trong máy?)")

    async def reset(self, box: Box, ip: str) -> None:
        """Hủy kết nối adb tới `ip` — gọi SAU khi container được tạo lại.

        Container bị xóa thì netns biến mất mà không gửi RST: kết nối TCP cũ treo
        nửa vĩnh viễn, `get-state` vẫn báo "device" (cached) nhưng mọi shell treo.
        Disconnect để lần connect sau mở TCP mới tới container mới.
        """
        async with self._lock:
            port = self.port_for(box, ip)
            serial = f"127.0.0.1:{port}"
            self._verified.discard(serial)
            await self._run("disconnect", serial, timeout=5)

    async def _with_retry(self, box: Box, ip: str, *args: str, timeout: float = 20) -> bytes:
        err = b""
        for attempt in (0, 1):
            serial = await self.serial(box, ip, force=attempt == 1)
            try:
                rc, out, err = await self._run("-s", serial, *args, timeout=timeout)
            except RemoteError:
                # timeout (shell treo trên TCP zombie của container đã xóa,
                # hoặc máy đang bận) → disconnect + connect lại rồi thử lần 2
                if attempt == 0:
                    log.info("adb %s timeout — mở lại kết nối", args[0])
                    self._verified.discard(serial)
                    await self._run("disconnect", serial, timeout=5)
                    continue
                raise
            if rc == 0:
                return out
            log.info("adb %s lỗi (%s), thử kết nối lại", args[0], err.decode(errors="replace").strip())
        raise RemoteError(f"adb {args[0]}: {err.decode(errors='replace').strip()[-300:]}")

    async def screencap(self, box: Box, ip: str) -> bytes:
        png = await self._with_retry(box, ip, "exec-out", "screencap", "-p", timeout=20)
        if not png.startswith(b"\x89PNG"):
            raise RemoteError("screencap không trả PNG (máy chưa boot xong?)")
        return png

    async def screenrecord(
        self, box: Box, ip: str, *, size: str = "", bitrate: int = 4_000_000,
        time_limit: int = 180,
    ) -> "asyncio.subprocess.Process":
        """Mở `screenrecord --output-format=h264 -` qua adbd; trả Process đang chạy.

        Caller đọc `proc.stdout` (H.264 Annex-B, phần cứng RK3588 encode) rồi
        `proc.kill()` khi xong. screenrecord tự dừng sau `time_limit` giây (tối đa
        180) nên caller cần khởi động lại để stream liên tục; mỗi lần bắt đầu luôn
        phát SPS/PPS/IDR nên decoder phía client bắt lại được ngay.

        Ít tốn băng thông hơn screencap ~25x (vài chục KB/s so với ~0.3MB mỗi
        frame PNG) nên không còn timeout khi kéo qua SSH tunnel.
        """
        serial = await self.serial(box, ip)
        rec = (
            "screenrecord --output-format=h264"
            f" --time-limit {max(1, min(time_limit, 180))}"
            + (f" --size {size}" if size else "")
            + f" --bit-rate {int(bitrate)} -"
        )
        # exec-out: không cấp pty, stdout là binary thuần (không bị \r\n hoá)
        return await asyncio.create_subprocess_exec(
            self.adb_bin, "-s", serial, "exec-out", rec,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def shell(self, box: Box, ip: str, *cmd: str, timeout: float = 20) -> str:
        return (await self._with_retry(box, ip, "shell", *cmd, timeout=timeout)).decode(errors="replace")

    async def push(self, box: Box, ip: str, local: str, remote: str, timeout: float = 120) -> str:
        """adb push local->remote (file or directory). Returns adb's output."""
        return (await self._with_retry(box, ip, "push", local, remote, timeout=timeout)).decode(errors="replace")
