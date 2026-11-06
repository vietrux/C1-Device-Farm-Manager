"""Integrity xoay vòng/status/remove — không cần device thật.

2026-11-06: luồng xoay vòng keybox nhúng (image layer + recreate) và status
đo SỰ THẬT (md5 lib chain → sổ chain → verdict), thay cho luồng magisk/pif chết.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import console.integrity as integrity_module
from console.integrity import Integrity, IntegrityError
from console.store import Box


def run(coro):
    return asyncio.run(coro)


class FakeAdb:
    def __init__(self, shell_out):
        self.pushes = []          # (local, remote)
        self.shells = []          # tuple(cmd)
        self._out = shell_out     # maps script basename -> output string

    async def push(self, box, ip, local, remote, timeout=120):
        self.pushes.append((local, remote))
        return "1 file pushed"

    async def shell(self, box, ip, *cmd, timeout=20):
        self.shells.append(cmd)
        script = cmd[-1].split("/")[-1] if cmd else ""
        return self._out.get(script, "{}")


class FakeRemote:
    """Trả lời agent theo (lệnh, arg đầu); ghi lại mọi lời gọi."""

    def __init__(self, answers: dict):
        self.answers = answers      # (cmd, first-arg) -> dict
        self.calls = []

    async def agent(self, box, *args, timeout=60, raw=False):
        self.calls.append(args)
        return self.answers.get((args[0], args[1] if len(args) > 1 else ""),
                                self.answers.get((args[0], None)))

    async def write_file(self, box, path, data, timeout=120):
        self.calls.append(("write_file", path))
        self.written = data
        return f"{len(data)} bytes"


class FakePool:
    def __init__(self, best=None, byname=None):
        self._best = best
        self._byname = byname or {}
        self.directory = Path("/kb")

    def best(self):
        return self._best

    def get(self, name):
        return self._byname.get(name)

    def list(self):
        items = [self._best] if self._best is not None else []
        items += [v for v in self._byname.values() if v is not self._best]
        return items


class KB:
    """KeyboxInfo giả: keys = [(algorithm, ok, errors)] — đủ cho rotate ghi sổ đúng."""

    def __init__(self, name, path, usable=True, root="google",
                 not_after="2033-05-20", revoked=False, keys=None):
        self.name = name
        self.path = path
        self.usable = usable
        self.not_after = not_after
        self.revoked = revoked
        if keys is None:
            keys = [("ecdsa", True, []), ("rsa", True, [])]
        self.keys = [type("K", (), {"algorithm": a, "ok": ok, "errors": errs,
                                    "root": root})() for a, ok, errs in keys]


BOX = Box(name="b1", ssh="u@h")

#: status.sh mới: sự thật thô (md5 lib chain + props + app) — không tự đoán "fixed"
STATUS_RAW = json.dumps({
    "chain_lib_md5": "81d32215d76260d81441ba3fc220ff25",
    "prop_fingerprint": "google/redfin/redfin:13/TQ3A/redfin:user/release-keys",
    "flash_locked": "1", "vbmeta_state": "locked",
    "magisk": "", "daemon": False, "zygisk": "",
    "pif_module": True, "pif_fingerprint": "google/tangorpro_beta/x",
    "keymaster_xml": True, "keymaster_xml_device": "kbx_ym_style_test",
    "keyattestation": True, "spic": False,
})


@pytest.fixture
def assets(tmp_path):
    d = tmp_path / "integrity-cache"
    d.mkdir()
    return d


def _integ(adb, pool, assets, remote=None, reg=None):
    return Integrity(adb, pool, remote=remote, assets_dir=assets,
                     registry_path=reg or assets / "chains.json")


def _seed_registry(path: Path, entries: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries))


# ------------------------------------------------------------------- status
def test_status_reads_registry_and_verdicts(assets):
    reg = assets / "chains.json"
    _seed_registry(reg, {"81d32215d76260d81441ba3fc220ff25": {
        "name": "kb-mau", "revoked": False, "expires": "2028-04-15"}})
    adb = FakeAdb({"status.sh": STATUS_RAW})
    st = run(_integ(adb, FakePool(), assets, reg=reg).status(BOX, "10.0.0.5"))
    assert st["chain"]["name"] == "kb-mau"
    assert st["device"] == "ok"
    assert st["fixed"] is True                       # chain sạch + props giả locked
    assert st["basic"]["fingerprint"].startswith("google/redfin")
    assert st["apps"] == {"keyattestation": True, "spic": False}
    # di sản không lẫn vào verdict
    assert st["legacy"]["pif_module"] is True
    assert st["legacy"]["keymaster_xml_device"] == "kbx_ym_style_test"


def test_status_revoked_chain(assets):
    reg = assets / "chains.json"
    _seed_registry(reg, {"81d32215d76260d81441ba3fc220ff25": {
        "name": "ym_full", "revoked": True, "expires": None}})
    adb = FakeAdb({"status.sh": STATUS_RAW})
    st = run(_integ(adb, FakePool(), assets, reg=reg).status(BOX, "10.0.0.5"))
    assert st["device"] == "revoked"
    assert st["fixed"] is False


def test_status_expired_chain(assets):
    reg = assets / "chains.json"
    _seed_registry(reg, {"81d32215d76260d81441ba3fc220ff25": {
        "name": "old", "revoked": False, "expires": "2020-01-01T00:00:00+00:00"}})
    adb = FakeAdb({"status.sh": STATUS_RAW})
    st = run(_integ(adb, FakePool(), assets, reg=reg).status(BOX, "10.0.0.5"))
    assert st["device"] == "expired"
    assert st["fixed"] is False


def test_status_unknown_and_factory(assets):
    raw = json.dumps({**json.loads(STATUS_RAW),
                      "chain_lib_md5": "dead0000beef"})
    adb = FakeAdb({"status.sh": raw})
    st = run(_integ(adb, FakePool(), assets).status(BOX, "10.0.0.5"))
    assert st["device"] == "unknown"
    assert st["fixed"] is False
    raw_factory = json.dumps({**json.loads(STATUS_RAW),
                              "chain_lib_md5": integrity_module.FACTORY_LIB_MD5})
    adb2 = FakeAdb({"status.sh": raw_factory})
    st2 = run(_integ(adb2, FakePool(), assets).status(BOX, "10.0.0.5"))
    assert st2["device"] == "factory"


def test_status_unlocked_props_not_fixed(assets):
    reg = assets / "chains.json"
    _seed_registry(reg, {"81d32215d76260d81441ba3fc220ff25": {
        "name": "kb-mau", "revoked": False, "expires": "2028-04-15"}})
    raw = json.dumps({**json.loads(STATUS_RAW), "flash_locked": ""})
    st = run(_integ(FakeAdb({"status.sh": raw}), FakePool(), assets,
                    reg=reg).status(BOX, "10.0.0.5"))
    assert st["device"] == "ok"                    # chain vẫn sạch…
    assert st["basic"]["ok"] is False              # …nhưng props chưa giả locked
    assert st["fixed"] is False


# ------------------------------------------------------------------- rotate
DEV_INFO = {"name": "testdev", "image": "dobox:test-image",
            "profile": "pixel5", "width": 720, "height": 1280,
            "dpi": 320, "fps": 24}
IMAGE_LIB = {"image": "dobox:test-image", "md5": "99a1fe5bbase0000000000",
             "b64": "AAAA"}     # fake — test thay _base_lib/embed bằng stub


def _rot_remote(patch_md5="81d32215d76260d81441ba3fc220ff25"):
    return FakeRemote({
        ("dev-info", "testdev"): dict(DEV_INFO),
        ("image-lib", "dobox:test-image"): dict(IMAGE_LIB),
        ("patch-image-lib", "dobox:test-image"): {"md5": patch_md5},
    })


def test_rotate_patches_image_and_registers(assets, tmp_path):
    kb = KB("kb-mau", "/kb/kb-mau.xml")
    reg = assets / "chains.json"
    integ = _integ(FakeAdb({}), FakePool(best=kb), assets, remote=_rot_remote(),
                   reg=reg)
    # stub: bỏ qua keybox_embed + agent b64 (đã test riêng ở tools)
    integ._base_libs["dobox:test-image"] = ("99a1fe5bbase0000000000", b"ELF" * 300)
    patched = tmp_path / "p.so"
    patched.write_bytes(b"\x7fELF" + b"P" * 1183164)
    integ._embed_keybox = lambda base, kb, md5, say: (patched, "81d32215d76260d81441ba3fc220ff25")
    logs = []
    info = run(integ.rotate(BOX, "testdev", None, logs.append))
    assert info["image"] == "dobox:test-image"
    assert info["profile"] == "pixel5"
    assert info["width"] == 720 and info["fps"] == 24
    # agent được gọi đúng trình tự: cấu hình máy → đẩy lib lên box → ghi layer
    cmds = [c[0] for c in integ.remote.calls]
    assert cmds == ["dev-info", "write_file", "patch-image-lib"]
    # sổ chain ghi md5 mới
    r = json.loads(reg.read_text())
    assert r["81d32215d76260d81441ba3fc220ff25"]["name"] == "kb-mau"
    assert any("kb-mau" in l for l in logs)


def test_rotate_md5_mismatch_raises(assets, tmp_path):
    kb = KB("kb-mau", "/kb/kb-mau.xml")
    integ = _integ(FakeAdb({}), FakePool(best=kb), assets,
                   remote=_rot_remote(patch_md5="BADBADBADBAD"), reg=assets / "chains.json")
    integ._base_libs["dobox:test-image"] = ("99a1fe5bbase0000000000", b"ELF" * 300)
    patched = tmp_path / "p.so"
    patched.write_bytes(b"\x7fELF" + b"P" * 1183164)
    integ._embed_keybox = lambda *a, **k: (patched, "81d32215d76260d81441ba3fc220ff25")
    with pytest.raises(IntegrityError, match="md5 khác kỳ vọng"):
        run(integ.rotate(BOX, "testdev", None))


def test_rotate_without_remote_raises(assets):
    kb = KB("kb-mau", "/kb/kb-mau.xml")
    integ = _integ(FakeAdb({}), FakePool(best=kb), assets, remote=None)
    with pytest.raises(IntegrityError, match="remote"):
        run(integ.rotate(BOX, "testdev", None))


def test_rotate_mixed_keybox_registers_ec_status(assets, tmp_path):
    """kb-mau-style: RSA revoked nhưng EC sạch → chain (serve EC) phải là SẠCH,
    không kế thừa cờ revoked của cả pool."""
    kb = KB("kb-mau", "/kb/kb-mau.xml", revoked=True,
            keys=[("ecdsa", True, []), ("rsa", False, ["KEY_COMPROMISE"])])
    reg = assets / "chains.json"
    integ = _integ(FakeAdb({}), FakePool(best=kb), assets, remote=_rot_remote(), reg=reg)
    integ._base_libs["dobox:test-image"] = ("99a1fe5bbase0000000000", b"ELF" * 300)
    patched = tmp_path / "p.so"
    patched.write_bytes(b"\x7fELF" + b"P" * 1183164)
    integ._embed_keybox = lambda *a, **k: (patched, "81d32215d76260d81441ba3fc220ff25")
    run(integ.rotate(BOX, "testdev", None))
    r = json.loads(reg.read_text())["81d32215d76260d81441ba3fc220ff25"]
    assert r["revoked"] is False
    assert "EC sạch" in r["note"] and "RSA revoked" in r["note"]


def test_rotate_ec_revoked_keybox_stays_revoked(assets, tmp_path):
    kb = KB("bad-ec", "/kb/bec.xml", revoked=True,
            keys=[("ecdsa", False, ["REVOKED"]), ("rsa", True, [])])
    reg = assets / "chains.json"
    integ = _integ(FakeAdb({}), FakePool(best=kb), assets, remote=_rot_remote(), reg=reg)
    integ._base_libs["dobox:test-image"] = ("99a1fe5bbase0000000000", b"ELF" * 300)
    patched = tmp_path / "p.so"
    patched.write_bytes(b"\x7fELF" + b"P" * 1183164)
    integ._embed_keybox = lambda *a, **k: (patched, "81d32215d76260d81441ba3fc220ff25")
    run(integ.rotate(BOX, "testdev", None))
    r = json.loads(reg.read_text())["81d32215d76260d81441ba3fc220ff25"]
    assert r["revoked"] is True


def test_rotate_unknown_keybox_still_raises(assets):
    integ = _integ(FakeAdb({}), FakePool(byname={}), assets, remote=_rot_remote())
    with pytest.raises(IntegrityError, match="no keybox"):
        run(integ.rotate(BOX, "testdev", "ghost"))


def test_rotate_revoked_keybox_is_allowed(assets, tmp_path):
    """Operator TỰ QUYẾT: keybox bị revoke theo danh sách Google vẫn xoay được
    (trickbox vẫn chạy ngon cả farm) — chỉ cảnh báo, registry ghi đúng revoked."""
    kb = KB("trickbox", "/kb/tb.xml", usable=False, revoked=True,
            keys=[("ecdsa", False, ["REVOKED: KEY_COMPROMISE"]), ("rsa", False, ["REVOKED: KEY_COMPROMISE"])])
    reg = assets / "chains.json"
    integ = _integ(FakeAdb({}), FakePool(byname={"trickbox": kb}), assets,
                   remote=_rot_remote(patch_md5="aaaa0123456789abcdef0123456789ab"), reg=reg)
    integ._base_libs["dobox:test-image"] = ("bbbb0123456789abcdef0123456789ab", b"ELF" * 300)
    patched = tmp_path / "p.so"
    patched.write_bytes(b"\x7fELF" + b"P" * 1183164)
    integ._embed_keybox = lambda *a, **k: (patched, "aaaa0123456789abcdef0123456789ab")
    logs = []
    info = run(integ.rotate(BOX, "testdev", "trickbox", logs.append))
    assert info["keybox"] == "trickbox"
    r = json.loads(reg.read_text())["aaaa0123456789abcdef0123456789ab"]
    assert r["revoked"] is True                 # ghi sổ trung thực
    assert any("vẫn xoay theo lựa chọn" in l for l in logs)   # nhưng không chặn


def test_auto_pick_falls_back_when_nothing_usable(assets, tmp_path):
    """Pool chỉ toàn revoked → tự động vẫn chọn keybox đầu (điểm cao nhất), kèm cảnh báo."""
    kb = KB("only-one", "/kb/oo.xml", usable=False, revoked=True,
            keys=[("ecdsa", False, ["REVOKED: KEY_COMPROMISE"]), ("rsa", False, ["REVOKED: KEY_COMPROMISE"])])
    integ = _integ(FakeAdb({}), FakePool(best=kb), assets,
                   remote=_rot_remote(patch_md5="aaaa0123456789abcdef0123456789ab"),
                   reg=assets / "chains.json")
    integ._base_libs["dobox:test-image"] = ("bbbb0123456789abcdef0123456789ab", b"ELF" * 300)
    patched = tmp_path / "p.so"
    patched.write_bytes(b"\x7fELF" + b"P" * 1183164)
    integ._embed_keybox = lambda *a, **k: (patched, "aaaa0123456789abcdef0123456789ab")
    logs = []
    info = run(integ.rotate(BOX, "testdev", None, logs.append))
    assert info["keybox"] == "only-one"
    assert any("revoked/hết hạn" in l for l in logs)

# ------------------------------------------------------------------- remove (legacy)
def test_remove_cleans_legacy(assets):
    raw = json.dumps({**json.loads(STATUS_RAW),
                      "chain_lib_md5": "81d32215d76260d81441ba3fc220ff25"})
    adb = FakeAdb({"remove.sh": "status: ...\nREMOVE_OK", "status.sh": raw})
    st = run(_integ(adb, FakePool(), assets).remove(BOX, "10.0.0.5"))
    assert any("remove.sh" in c[-1] for c in adb.shells)
    assert st["device"] == "unknown"          # chưa ghi sổ → trung thực


def test_status_bad_json_raises(assets):
    adb = FakeAdb({"status.sh": "boom"})
    with pytest.raises(IntegrityError, match="no JSON"):
        run(_integ(adb, FakePool(), assets).status(BOX, "10.0.0.5"))
