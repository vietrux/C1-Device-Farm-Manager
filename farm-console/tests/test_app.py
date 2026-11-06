"""API của farm-console với Remote giả (không SSH thật)."""

from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path
from typing import Any, List

import pytest
from fastapi.testclient import TestClient

from console.app import create_app
from console.remote import Remote, RemoteError
from console.store import BoxStore, StoreError

SCRIPTS = Path(__file__).resolve().parents[2] / "tools" / "box"


class FakeRemote:
    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.boot_after = 2
        self.boot_polls = 0
        self.load = 0.5
        self.list_devices: List[dict] = []
        self.fail_list_for: set = set()

    async def agent(self, box, *args: str, timeout: float = 60, raw: bool = False) -> Any:
        self.calls.append(args)
        cmd = args[0]
        if cmd == "info":
            return {"hostname": "b", "load": [0.1, 0.1, 0.1], "cpus": 8}
        if cmd == "list":
            if box.name in self.fail_list_for:
                raise RemoteError("ssh không được")
            return {"devices": self.list_devices, "unsafe": []}
        if cmd == "profiles":
            return {"profiles": [{"key": "pixel6", "model": "Pixel 6",
                                  "device": "oriole", "fingerprint": "google/oriole/..."}]}
        if cmd == "create":
            return {"name": args[1], "ip": "172.30.0.50", "binders": [1, 2, 3], "image": "x/dobox:Q12", "id": "abc"}
        if cmd == "boot":
            self.boot_polls += 1
            return {"running": True, "boot_completed": self.boot_polls >= self.boot_after,
                    "load": [self.load, 0, 0], "ip": "172.30.0.50"}
        if cmd == "start" and args[1] == "bad":
            raise RemoteError("boom")
        return {"ok": True}

    async def host_script(self, box, args, on_line, timeout: float = 600) -> int:
        self.calls.append(("host", *args))
        on_line("Kiểm tra:")
        on_line("  [ OK ] docker   DOCKER_OPTS=...")
        return 0

    async def install_resetprop(self, box, on_line):
        self.calls.append(("resetprop",))
        on_line("resetprop: đã có sẵn trên box")
        return True

    async def ensure_image(self, box, image, on_line, timeout: float = 1800):
        self.calls.append(("ensure-image", image))
        on_line(f"image chính {image}: đã có sẵn trên box")
        return True

    async def check_ssh(self, box):
        return None


class FakeAdb:
    available = True

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def refresh_slots(self, boxes) -> None:  # Adb thật gán slot port theo config
        pass

    async def screencap(self, box, ip):
        self.calls.append(("screencap", ip))
        return b"\x89PNGdata"

    async def shell(self, box, ip, *cmd):
        self.calls.append(("shell", ip, *cmd))
        return ""

    async def screenrecord(self, box, ip, *, size="", bitrate=4_000_000, time_limit=180):
        self.calls.append(("screenrecord", ip, size, bitrate))
        # lần 2 dừng vòng restart (TestClient không báo disconnect)
        n = sum(1 for x in self.calls if x[0] == "screenrecord")
        if n > 1:
            raise RemoteError("stream kết thúc (test)")
        return _FakeProc(b"\x00\x00\x00\x01\x67\x42\xc0\x1f" + b"h264")


class _FakeProc:
    """Giả asyncio subprocess: stdout trả 1 chunk rồi EOF."""
    def __init__(self, data: bytes) -> None:
        self.returncode = None
        self.stdout = self._Stdout(data, self)

    class _Stdout:
        def __init__(self, data, proc):
            self._data = data
            self._proc = proc
        async def read(self, n):
            d, self._data = self._data[:n], self._data[n:]
            if not d:
                self._proc.returncode = 0
            return d

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("console.app.asyncio.sleep", _nosleep)
    store = BoxStore.load(tmp_path / "boxes.yaml")
    remote = FakeRemote()
    key = tmp_path / "adbkey.pub"
    key.write_text("QAAAAkey user@laptop\n")
    fadb = FakeAdb()
    app = create_app(store, remote, adbkey_path=key, adb=fadb)  # type: ignore[arg-type]
    c = TestClient(app)
    c.adb = fadb  # type: ignore[attr-defined]
    return c, store, remote, tmp_path


async def _nosleep(_s):
    return None


def wait_job(client, jid, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{jid}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("job chưa xong")


def test_store_roundtrip_and_permissions(tmp_path):
    s = BoxStore.load(tmp_path / "b.yaml")
    s.add("box-1", "user@10.0.0.1", "secret", "kệ 2")
    mode = stat.S_IMODE(os.stat(tmp_path / "b.yaml").st_mode)
    assert mode == 0o600
    s2 = BoxStore.load(tmp_path / "b.yaml")
    assert s2.get("box-1").sudo_password == "secret"
    assert "sudo_password" not in s2.get("box-1").public()
    with pytest.raises(StoreError):
        s2.add("box-1", "user@x")
    with pytest.raises(StoreError):
        s2.add("bad name", "user@x")
    with pytest.raises(StoreError):
        s2.add("ok", "user@x; rm -rf /")


def test_add_list_remove_box_never_leaks_password(env):
    c, store, _, _ = env
    r = c.post("/api/boxes", json={"name": "b1", "ssh": "user@1.2.3.4", "sudo_password": "secret-test-pw"})
    assert r.status_code == 200 and r.json()["ssh_error"] is None
    assert "secret-test-pw" not in c.get("/api/boxes").text
    assert c.delete("/api/boxes/b1").status_code == 200
    assert c.get("/api/boxes").json() == []


def test_fleet_all_boxes_in_one_call(env):
    """GET /api/fleet: gộp máy của mọi box trong 1 request; box lỗi vẫn 200 kèm error."""
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    store.add("b2", "user@h2", "p")
    remote.list_devices = [{"name": "phone-1", "vendor": False, "state": "running"}]
    remote.fail_list_for = {"b2"}
    r = c.get("/api/fleet").json()
    assert [b["name"] for b in r["boxes"]] == ["b1", "b2"]
    b1 = next(b for b in r["boxes"] if b["name"] == "b1")
    b2 = next(b for b in r["boxes"] if b["name"] == "b2")
    assert b1["devices"][0]["name"] == "phone-1"     # gộp được
    assert b1.get("error") is None
    assert "error" in b2 and b2["devices"] == []      # box lỗi vẫn hiện, không sập cả cụm


def test_unknown_box_404(env):
    c, *_ = env
    assert c.get("/api/boxes/nope/info").status_code == 404


def test_provision_apply_is_one_button_takeover(env):
    """Nút Chuẩn hoá = trọn gói: apply --takeover + resetprop + image chính."""
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    store.get("b1").main_image = "dobox:Q12_gms_mau"
    j = c.post("/api/boxes/b1/provision/apply").json()
    done = wait_job(c, j["id"])
    assert done["status"] == "ok", done["lines"]
    assert ("host", "apply", "--takeover") in remote.calls
    assert ("resetprop",) in remote.calls
    assert ("ensure-image", "dobox:Q12_gms_mau") in remote.calls
    assert any("image chính" in l for l in done["lines"])
    # bí danh cũ takeover vẫn chạy như apply
    j2 = c.post("/api/boxes/b1/provision/takeover").json()
    assert wait_job(c, j2["id"])["status"] == "ok"
    assert c.post("/api/boxes/b1/provision/rm-rf").status_code == 400


def test_provision_apply_without_main_image_skips_push(env):
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    j = c.post("/api/boxes/b1/provision/apply").json()
    done = wait_job(c, j["id"])
    assert done["status"] == "ok"
    assert not any(a[0] == "ensure-image" for a in remote.calls)
    assert any("chưa đặt main_image" in l for l in done["lines"])


def test_boxes_payload_has_main_image(env):
    c, store, *_ = env
    store.add("b1", "user@h", "p")
    store.get("b1").main_image = "dobox:Q12_gms_mau"
    store.add("b2", "user@h2", "p")
    store.default_main_image = "dobox:Q12_gms_macdinh"
    boxes = {b["name"]: b for b in c.get("/api/boxes").json()}
    assert boxes["b1"]["main_image"] == "dobox:Q12_gms_mau"     # override từng box
    assert boxes["b2"]["main_image"] == "dobox:Q12_gms_macdinh" # mặc định chung


def test_create_device_waits_boot_then_autostart(env):
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    j = c.post("/api/boxes/b1/devices", json={"name": "phone-1"}).json()
    done = wait_job(c, j["id"])
    assert done["status"] == "ok", done["lines"]
    create = next(a for a in remote.calls if a[0] == "create")
    assert "--adbkey" in create and "QAAAAkey user@laptop" in create
    assert ("autostart", "phone-1", "on") in remote.calls


def test_create_device_passes_profile(env):
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    j = c.post("/api/boxes/b1/devices", json={"name": "p1", "profile": "pixel6"}).json()
    assert wait_job(c, j["id"])["status"] == "ok"
    create = next(a for a in remote.calls if a[0] == "create")
    assert "--profile" in create and create[create.index("--profile") + 1] == "pixel6"


def test_box_profiles_endpoint(env):
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    r = c.get("/api/boxes/b1/profiles").json()
    assert r["profiles"][0]["model"] == "Pixel 6"


def test_create_batch_makes_numbered_devices(env):
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    j = c.post("/api/boxes/b1/devices", json={"name": "farm", "count": 3}).json()
    done = wait_job(c, j["id"])
    assert done["status"] == "ok", done["lines"]
    created = [a[1] for a in remote.calls if a[0] == "create"]
    assert created == ["farm-1", "farm-2", "farm-3"]
    assert done["result"]["created"] == ["farm-1", "farm-2", "farm-3"]
    assert done["result"]["failed"] == []


def test_create_batch_skips_existing_names(env):
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    remote.existing = ["farm-1", "farm-2"]

    async def agent(box, *args, timeout=60, raw=False):
        remote.calls.append(args)
        if args[0] == "list":
            return {"devices": [{"name": n} for n in remote.existing], "unsafe": []}
        if args[0] == "create":
            return {"name": args[1], "ip": "172.30.0.50", "binders": [1], "image": "x/y", "id": "a"}
        if args[0] == "boot":
            return {"running": True, "boot_completed": True, "load": [0.5, 0, 0], "ip": "172.30.0.50"}
        return {"ok": True}
    remote.agent = agent
    j = c.post("/api/boxes/b1/devices", json={"name": "farm", "count": 2}).json()
    done = wait_job(c, j["id"])
    assert done["status"] == "ok", done["lines"]
    created = [a[1] for a in remote.calls if a[0] == "create"]
    assert created == ["farm-3", "farm-4"]


def test_create_batch_removes_failed_to_free_slot(env):
    # máy boot không xong (timeout) phải bị XOÁ để trả slot cho máy sau
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")

    async def agent(box, *args, timeout=60, raw=False):
        remote.calls.append(args)
        if args[0] == "info":
            return {"load": [0.5, 0, 0], "cpus": 8}
        if args[0] == "list":
            return {"devices": [], "unsafe": []}
        if args[0] == "create":
            return {"name": args[1], "ip": "172.30.0.50", "binders": [1], "image": "x/y", "id": "a"}
        if args[0] == "boot":
            return {"running": True, "boot_completed": False, "load": [0.5, 0, 0], "ip": "172.30.0.50"}
        return {"ok": True}
    remote.agent = agent
    j = c.post("/api/boxes/b1/devices", json={"name": "farm", "count": 2}).json()
    done = wait_job(c, j["id"])
    assert done["status"] == "failed"          # không máy nào boot
    rms = [a for a in remote.calls if a[0] == "rm"]
    assert ("rm", "farm-1", "--purge") in remote.calls
    assert ("rm", "farm-2", "--purge") in remote.calls


def test_create_device_load_guard_stops(env):
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    remote.load = 50          # box quá tải thật sự, cao liên tục
    remote.boot_after = 999   # không bao giờ boot xong → strikes tích lũy → dừng
    j = c.post("/api/boxes/b1/devices", json={"name": "phone-2", "autostart": True}).json()
    done = wait_job(c, j["id"])
    assert done["status"] == "failed"
    assert any("liên tục" in l for l in done["lines"])
    assert ("stop", "phone-2") in remote.calls
    assert not any(a[0] == "autostart" for a in remote.calls)


def test_create_device_survives_boot_spike(env):
    # load spike nhất thời (1 lần) KHÔNG được giết máy: boot vẫn xong
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    remote.load = 50          # cao, nhưng boot xong ở poll thứ 2 (trước khi đủ strikes)
    remote.boot_after = 2
    j = c.post("/api/boxes/b1/devices", json={"name": "phone-3"}).json()
    done = wait_job(c, j["id"])
    assert done["status"] == "ok", done["lines"]
    assert not any(a[0] == "stop" for a in remote.calls)


def test_create_device_rejects_bad_name(env):
    c, store, *_ = env
    store.add("b1", "user@h", "p")
    assert c.post("/api/boxes/b1/devices", json={"name": "Bad Name"}).status_code == 400


def test_device_actions_and_errors(env):
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    assert c.post("/api/boxes/b1/devices/d1/stop").status_code == 200
    assert c.post("/api/boxes/b1/devices/d1/autostart-off").status_code == 200
    assert ("autostart", "d1", "off") in remote.calls
    r = c.post("/api/boxes/b1/devices/bad/start")
    assert r.status_code == 502 and "boom" in r.json()["detail"]
    assert c.post("/api/boxes/b1/devices/d1/explode").status_code == 400


def test_screen_and_input(env):
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    r = c.get("/api/boxes/b1/devices/d1/screen")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert ("screencap", "172.30.0.50") in c.adb.calls
    assert c.post("/api/boxes/b1/devices/d1/input", json={"type": "tap", "x": 1, "y": 2}).status_code == 200
    assert ("shell", "172.30.0.50", "input", "tap", "1", "2") in c.adb.calls
    assert c.post("/api/boxes/b1/devices/d1/input", json={"type": "text", "text": "xin chao"}).status_code == 200
    assert ("shell", "172.30.0.50", "input", "text", "xin%schao") in c.adb.calls
    # chặn chèn lệnh shell vào adb shell
    for bad in ({"type": "tap"}, {"type": "text", "text": "a; reboot"}, {"type": "text", "text": "$(id)"},
                {"type": "key", "key": "HOME;reboot"}, {"type": "text", "text": "a`id`"}):
        assert c.post("/api/boxes/b1/devices/d1/input", json=bad).status_code == 400, bad


def test_stream_h264(env):
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    r = c.get("/api/boxes/b1/devices/d1/stream.h264?size=480x854&bitrate=2000000")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/h264"
    assert r.content.startswith(b"\x00\x00\x00\x01\x67")  # Annex-B SPS
    assert ("screenrecord", "172.30.0.50", "480x854", 2000000) in c.adb.calls


def test_stream_h264_rejects_bad_params(env):
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    assert c.get("/api/boxes/b1/devices/d1/stream.h264?size=evil").status_code == 422
    assert c.get("/api/boxes/b1/devices/d1/stream.h264?bitrate=100").status_code == 422


def test_delete_purge_flag(env):
    c, store, remote, _ = env
    store.add("b1", "user@h", "p")
    c.delete("/api/boxes/b1/devices/d1?purge=true")
    assert ("rm", "d1", "--purge") in remote.calls


def test_token_required(tmp_path):
    store = BoxStore.load(tmp_path / "b.yaml")
    app = create_app(store, FakeRemote(), token="s3cret")  # type: ignore[arg-type]
    c = TestClient(app)
    assert c.get("/api/boxes").status_code == 401
    assert c.get("/api/boxes", headers={"X-Token": "s3cret"}).status_code == 200
    assert c.get("/api/boxes?token=s3cret").status_code == 200
    assert c.get("/").status_code in (200, 503)  # trang web không cần token, chỉ API cần


def test_remote_command_building(tmp_path):
    from console.store import Box
    r = Remote(SCRIPTS, tmp_path / "cm")
    withpw = Box("b", "user@h", "pw")
    nopw = Box("b", "user@h")
    assert Remote.sudo_cmd(withpw, "python3 -").startswith("sudo -S -p ''")
    assert Remote.sudo_cmd(nopw, "python3 -").startswith("sudo -n")
    assert Remote.stdin_for(withpw, b"code") == b"pw\ncode"
    assert Remote.stdin_for(nopw, b"code") == b"code"
    argv = r.ssh_argv(withpw, "true")
    assert "BatchMode=yes" in argv and argv[-2:] == ["user@h", "true"]
    assert r.agent_path.is_file() and r.host_path.is_file()
