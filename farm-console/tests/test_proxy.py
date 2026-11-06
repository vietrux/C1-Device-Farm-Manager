"""Proxy.set/clear/status against a fake adb — checks the extservice call shape."""

from __future__ import annotations

import asyncio

import pytest

from console.proxy import Proxy, ProxyError
from console.store import Box


def run(coro):
    return asyncio.run(coro)


class FakeAdb:
    def __init__(self, out):
        self.pushes = []
        self.shells = []
        self._out = out

    async def push(self, box, ip, local, remote, timeout=120):
        self.pushes.append((local, remote))

    async def shell(self, box, ip, *cmd, timeout=20):
        self.shells.append(cmd)
        return self._out


BOX = Box(name="b1", ssh="u@h")
OK_SET = '{"applied":true,"ip":"1.2.3.4","port":"1080","type":"2","raw":"ok"}\nPROXY_OK'
OK_CLR = '{"applied":false,"ip":"","port":"","type":"","raw":"ok ok"}\nPROXY_OK'
OK_STAT = '{"applied":null,"ip":"","port":"","type":"","raw":"cmd=1 resp"}\nPROXY_OK'


def test_set_socks5_passes_args():
    adb = FakeAdb(OK_SET)
    p = Proxy(adb)
    st = run(p.set(BOX, "10.0.0.5", host="1.2.3.4", port=1080,
                   kind="socks5", user="u", password="pw"))
    # device script invoked as: sh <remote> set <type> <ip> <port> <user> <pass>
    cmd = adb.shells[-1]
    assert cmd[:2] == ("sh", "/data/local/tmp/proxy.sh")
    assert cmd[2:] == ("set", "2", "1.2.3.4", "1080", "u", "pw")
    assert st["applied"] is True and st["ip"] == "1.2.3.4"


def test_set_http_type():
    adb = FakeAdb(OK_SET)
    run(Proxy(adb).set(BOX, "10.0.0.5", host="h", port=8080, kind="http"))
    assert adb.shells[-1][3] == "1"  # http -> type 1


def test_set_bad_kind():
    with pytest.raises(ProxyError, match="unknown proxy type"):
        run(Proxy(FakeAdb(OK_SET)).set(BOX, "10.0.0.5", host="h", port=1, kind="vpn"))


def test_set_requires_host_port():
    with pytest.raises(ProxyError, match="host and port"):
        run(Proxy(FakeAdb(OK_SET)).set(BOX, "10.0.0.5", host="", port=0))


def test_set_fail_raises():
    adb = FakeAdb('{"applied":true}\nPROXY_FAIL: upstream unreachable')
    with pytest.raises(ProxyError, match="unreachable"):
        run(Proxy(adb).set(BOX, "10.0.0.5", host="h", port=1))


def test_clear():
    adb = FakeAdb(OK_CLR)
    st = run(Proxy(adb).clear(BOX, "10.0.0.5"))
    assert st["applied"] is False
    assert adb.shells[-1][2] == "clear"


def test_status():
    adb = FakeAdb(OK_STAT)
    st = run(Proxy(adb).status(BOX, "10.0.0.5"))
    assert st["applied"] is None
    assert adb.shells[-1][2] == "status"


def test_no_json_raises():
    with pytest.raises(ProxyError, match="no JSON"):
        run(Proxy(FakeAdb("garbage\nPROXY_OK")).status(BOX, "10.0.0.5"))
