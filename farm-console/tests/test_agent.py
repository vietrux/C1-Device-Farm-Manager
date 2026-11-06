"""Phần thuần (không I/O) của tools/box/df_agent.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[2] / "tools" / "box" / "df_agent.py"
spec = importlib.util.spec_from_file_location("df_agent", AGENT)
agent = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(agent)


def container(name, binders=(), mali=True, bind=True, privileged=False, ip=None):
    devs = [{"PathOnHost": f"/dev/binder{b}"} for b in binders]
    if mali:
        devs.append({"PathOnHost": "/dev/mali0"})
    binds = ["/mmc/devicefarm/data/%s:/data" % name] + (["/dev/mali0:/dev/mali0"] if bind else [])
    nets = {"df-br": {"IPAddress": ip or "", "IPAMConfig": {"IPv4Address": ip} if ip else None}}
    return {"Name": "/" + name, "HostConfig": {"Devices": devs, "Binds": binds, "Privileged": privileged},
            "NetworkSettings": {"Networks": nets}}


def test_pick_binders_first_free_trio():
    assert agent.pick_binders(range(1, 37), []) == (1, 2, 3)
    assert agent.pick_binders(range(1, 37), [1, 2, 3]) == (4, 5, 6)
    # trio bị dùng một phần cũng bỏ qua
    assert agent.pick_binders(range(1, 37), [5]) == (1, 2, 3)
    assert agent.pick_binders(range(1, 37), [1, 5]) == (7, 8, 9)


def test_pick_binders_exhausted():
    with pytest.raises(agent.AgentError):
        agent.pick_binders([1, 2, 3], [2])
    with pytest.raises(agent.AgentError):
        agent.pick_binders([], [])


def test_pick_ip_skips_used():
    assert agent.pick_ip("172.30.0.0/24", [], start=50) == "172.30.0.50"
    assert agent.pick_ip("172.30.0.0/24", ["172.30.0.50", "172.30.0.51"], start=50) == "172.30.0.52"


def test_run_args_always_bind_mali_and_not_privileged():
    a = agent.build_run_args("phone-1", "img:tag", "172.30.0.60", (4, 5, 6), "/mmc/devicefarm/data/phone-1",
                             720, 1280, 320, 24)
    s = " ".join(a)
    assert "-v /dev/mali0:/dev/mali0" in s
    assert "--privileged" not in s and "--memory" not in s and "--restart" not in s
    assert "--device /dev/binder4:/dev/binder" in s
    assert "--device /dev/binder6:/dev/vndbinder" in s
    assert "--ip 172.30.0.60" in s
    assert a.count("--cap-add") == 17
    i = a.index("img:tag")
    assert a[i + 1:i + 3] == ["androidboot.hardware=rk30board", "qemu=1"]
    assert "androidboot.dobox_width=720" in a


def test_unsafe_gpu_containers():
    cs = [container("ok", (1, 2, 3)), container("nobind", (4, 5, 6), bind=False),
          container("priv", privileged=True, mali=False, bind=False), container("nogpu", mali=False, bind=False)]
    assert agent.unsafe_gpu_containers(cs) == ["nobind", "priv"]
    assert agent.unsafe_gpu_containers(cs, exclude="nobind") == ["priv"]


def test_container_helpers():
    c = container("x", (7, 8, 9), ip="172.30.0.57")
    assert agent.binders_of(c) == [7, 8, 9]
    assert agent.ip_on_network(c) == "172.30.0.57"
    assert agent.data_dir_of(c) == "/mmc/devicefarm/data/x"
    assert agent.mali_bound(c)


def test_vendor_name_pattern():
    assert agent.VENDOR_RE.match("8e306f02b547f19c6f3687b52f90dfb3_2_T1002")
    assert agent.VENDOR_RE.match("myt_sdk")
    assert not agent.VENDOR_RE.match("df-v1")


def test_main_unknown_command(capsys):
    assert agent.main(["nope"]) == 2
    assert "error" in capsys.readouterr().out


# --------------------------------------------------------------------------- device profiles
def test_profiles_pool_coherent():
    assert agent.PROFILE_KEYS, "pool không rỗng"
    for key in agent.PROFILE_KEYS:
        p = agent.PROFILES[key]
        fp = agent.profile_fingerprint(p)
        # fingerprint nhất quán: brand/device/device:release/build_id/incremental
        assert fp == (f'{p["brand"]}/{p["device"]}/{p["device"]}:{p["release"]}/'
                      f'{p["build_id"]}/{p["incremental"]}:user/release-keys')
        assert p["release"] == "12"  # phải khớp framework Android 12 (API 31)


def test_choose_profile_explicit_auto_random():
    k = agent.PROFILE_KEYS[0]
    assert agent.choose_profile("n01", k) == k            # chỉ định cụ thể
    # auto: ổn định theo tên
    assert agent.choose_profile("n01", "auto") == agent.choose_profile("n01", None)
    assert agent.choose_profile("n01", "auto") in agent.PROFILES
    # random: thuộc pool
    assert agent.choose_profile("n01", "random") in agent.PROFILES
    with pytest.raises(agent.AgentError):
        agent.choose_profile("n01", "khong-co")


def test_gen_serial_stable_and_unique():
    assert agent.gen_serial("n01") == agent.gen_serial("n01")   # ổn định
    assert agent.gen_serial("n01") != agent.gen_serial("n08")   # khác máy khác serial
    s = agent.gen_serial("n01")
    assert len(s) == 16 and s == s.upper()


def test_render_profile_sets_coherent_props():
    key = "pixel6"
    pairs = agent.render_profile(key, "DEADBEEF12345678")
    d = dict(pairs)
    fp = agent.profile_fingerprint(agent.PROFILES[key])
    # fingerprint ở mọi partition
    for prop in ("ro.build.fingerprint", "ro.system.build.fingerprint",
                 "ro.vendor.build.fingerprint", "ro.product.build.fingerprint"):
        assert d[prop] == fp
    assert d["ro.product.model"] == "Pixel 6"
    assert d["ro.product.system.model"] == "Pixel 6"
    assert d["ro.build.version.release"] == "12"
    assert d["ro.serialno"] == "DEADBEEF12345678"
    assert d["ro.boot.serialno"] == "DEADBEEF12345678"
    # không có giá trị nào chứa nháy đơn (an toàn khi nối vào lệnh sh)
    assert not any("'" in v for _, v in pairs)


def test_cmd_profiles_lists_pool():
    out = agent.cmd_profiles([])
    keys = [p["key"] for p in out["profiles"]]
    assert keys == agent.PROFILE_KEYS
    assert all("fingerprint" in p for p in out["profiles"])


def test_gen_imei_deterministic_luhn():
    # vector chuẩn Luhn (IMEI hợp lệ 490154203237518 trên Wikipedia)
    assert agent.luhn_check_digit("49015420323751") == "8"
    a, b = agent.gen_imei("n01"), agent.gen_imei("n01")
    assert a == b and len(a) == 15 and a.isdigit() and a.startswith("35")
    assert agent.gen_imei("n02") != a  # mỗi máy một IMEI
    total = 0
    for i, ch in enumerate(reversed(a)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    assert total % 10 == 0
