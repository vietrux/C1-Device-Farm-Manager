"""Adb: cổng cố định theo (box, IP) và không bao giờ trả serial trỏ nhầm máy."""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from console.adb import PORT_BASE, Adb
from console.remote import RemoteError
from console.store import Box


class FakeRemote:
    def __init__(self) -> None:
        self.forwards: List[tuple] = []
        self.resets = 0

    async def forward(self, box, lport, host, hport):
        self.forwards.append((lport, host))

    async def reset_master(self, box):
        self.resets += 1


class FakeAdb(Adb):
    """adb giả: mỗi cổng 'trỏ' tới IP trong self.target."""

    def __init__(self, remote, target):
        super().__init__(remote, adb_bin="true")
        self.target = target          # port -> ip máy thật phía sau
        self.fix_after_reset = None   # port -> ip sau khi reset master

    @property
    def available(self) -> bool:
        return True

    async def _run(self, *args, timeout=20):
        if args[0] == "connect":
            return 0, b"connected to x", b""
        if args[0] == "disconnect":
            return 0, b"", b""
        port = int(args[1].split(":")[1])
        if args[2] == "get-state":
            return 0, b"device\n", b""
        if args[2:5] == ("shell", "ip", "-4"):
            if self.remote.resets and self.fix_after_reset:
                self.target.update(self.fix_after_reset)
            return 0, f"9: rmnet0    inet {self.target.get(port)}/24 scope global".encode(), b""
        return 0, b"", b""


def test_port_is_deterministic_across_restarts():
    box = Box("b", "user@test-box")
    p1 = Adb(FakeRemote()).port_for(box, "172.30.0.54")
    p2 = Adb(FakeRemote()).port_for(box, "172.30.0.54")  # "console khởi động lại"
    assert p1 == p2 and (p1 - PORT_BASE) % 256 == 54
    assert Adb(FakeRemote()).port_for(box, "172.30.0.50") != p1


def test_wrong_device_triggers_reset_then_ok():
    box = Box("b", "user@h")
    r = FakeRemote()
    a = FakeAdb(r, {})
    port = a.port_for(box, "172.30.0.50")
    a.target[port] = "172.30.0.54"                     # forward cũ trỏ nhầm sang df-b
    a.fix_after_reset = {port: "172.30.0.50"}
    serial = asyncio.run(a.serial(box, "172.30.0.50"))
    assert serial.endswith(str(port)) and r.resets == 1


def test_wrong_device_never_returned():
    box = Box("b", "user@h")
    r = FakeRemote()
    a = FakeAdb(r, {})
    port = a.port_for(box, "172.30.0.50")
    a.target[port] = "172.30.0.54"                     # reset cũng không sửa được
    with pytest.raises(RemoteError):
        asyncio.run(a.serial(box, "172.30.0.50"))


# ------------------------------------------------------------- cấp key tự động
class KeyGrantRemote(FakeRemote):
    """Ghi lại lời gọi agent adbkey-add (console tự cứu khi máy không biết key)."""

    def __init__(self) -> None:
        super().__init__()
        self.agent_calls: List[tuple] = []

    async def agent(self, box, *args, timeout=60, raw=False):
        self.agent_calls.append(args)
        return {"name": "d", "added": True}


class UnauthorizedThenOkAdb(FakeAdb):
    """Lần connect đầu bị từ chối (máy chưa có key của console này); sau khi
    agent cấp key thì connect thành công — kịch bản laptop đồng nghiệp."""

    def __init__(self, remote, target):
        super().__init__(remote, target)
        self.connects = 0

    async def _connect(self, serial, ip):
        self.connects += 1
        if self.connects == 1:
            raise RemoteError("adb chưa được máy cho phép (key adb của console chưa có trong máy?)")
        await super()._connect(serial, ip)


def test_unauthorized_auto_grants_key_then_connects():
    async def go():
        remote = KeyGrantRemote()
        box = Box(name="b1", ssh="u@h")
        adb = UnauthorizedThenOkAdb(remote, {})
        adb.target[adb.port_for(box, "10.0.0.50")] = "10.0.0.50"
        adb.key_pub = "AAAAB3NzaC1... testkey"
        serial = await adb.serial(box, "10.0.0.50")
        assert serial.startswith("127.0.0.1:")
        assert remote.agent_calls[0][0] == "adbkey-add"
        assert remote.agent_calls[0][1] == "10.0.0.50"
        assert remote.agent_calls[0][2] == adb.key_pub
        assert adb.connects == 2
    asyncio.run(go())


def test_unauthorized_without_key_raises_without_agent_call():
    async def go():
        remote = KeyGrantRemote()
        box = Box(name="b1", ssh="u@h")
        adb = UnauthorizedThenOkAdb(remote, {})
        adb.target[adb.port_for(box, "10.0.0.50")] = "10.0.0.50"
        adb.key_pub = None
        with pytest.raises(RemoteError, match="chưa được máy cho phép"):
            await adb.serial(box, "10.0.0.50")
        assert remote.agent_calls == []
    asyncio.run(go())
