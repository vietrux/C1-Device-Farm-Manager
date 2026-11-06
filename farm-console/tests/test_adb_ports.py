"""Slot port tunnel của Adb — cái bug "tunnel trùng nhau từ 2 box trở lên".

Trước đây: slot = crc32(box.ssh) % 100, xung đột dịch +256 THEO THỨ TỰ CẤP
PHÁT → console khởi động lại với thứ tự khác là port hai box hoán đổi, serial
trỏ nhầm máy, reset_master dọn dồn. Giờ slot gán theo THỨ TỰ CONFIG, restart
bao nhiêu lần port cũng y hệt.
"""

from pathlib import Path

import pytest

from console.adb import Adb, PORT_BASE, PORT_SLOTS
from console.store import Box


def make_boxes(*addrs):
    return [Box(name=f"box-{i}", ssh=a) for i, a in enumerate(addrs)]


def new_adb(boxes):
    adb = Adb(remote=None)  # remote chỉ cần cho serial()/forward — không dùng ở đây
    adb.refresh_slots(boxes)
    return adb


IP = "172.30.0.50"  # cả hai box đều có container IP này (mỗi box một network riêng)


def test_two_boxes_same_ip_different_ports():
    """Cùng IP container trên 2 box → 2 port khác nhau, không đè nhau."""
    b1, b2 = make_boxes("user@192.168.0.9", "user@192.168.0.10")
    adb = new_adb([b1, b2])
    assert adb.port_for(b1, IP) != adb.port_for(b2, IP)


def test_restart_same_config_same_ports():
    """Console khởi động lại (Adb mới) — port của mọi (box, ip) không đổi."""
    boxes = make_boxes("user@192.168.0.9", "user@192.168.0.10", "user@100.99.1.7")
    a1 = new_adb(boxes)
    a2 = new_adb(boxes)
    for b in boxes:
        # thứ tự chạm khác nhau cũng không ảnh hưởng (a2 chạm ngược thứ tự)
        assert a1.port_for(b, IP) == a2.port_for(b, IP)
    rev = new_adb(list(reversed(boxes)))
    for b in boxes:
        assert rev.port_for(b, IP) == a1.port_for(b, IP)


def test_crc32_collision_resolved_deterministically(monkeypatch):
    """Hai box crc32 trùng slot (buộc bằng monkeypatch) → slot khác nhau,
    vàRestart mấy lần cũng ra đúng cùng cặp slot đó."""
    real_crc = __import__("zlib").crc32
    fake_addrs = {"user@a", "user@b"}

    def crc32(data, _=0):
        s = data.decode()
        if s in fake_addrs:
            return 7  # ép trùng slot
        return real_crc(data)

    monkeypatch.setattr("console.adb.zlib.crc32", crc32)
    ba, bb = Box(name="a", ssh="user@a"), Box(name="b", ssh="user@b")
    other = Box(name="z", ssh="user@zzz")

    a1 = new_adb([ba, bb, other])
    s1 = dict(a1._slots)
    assert len(set(s1.values())) == 3  # không slot nào đè nhau

    a2 = new_adb([ba, bb, other])      # restart
    assert a2._slots == s1
    a3 = new_adb([other, bb, ba])      # config viết thứ tự khác → vẫn phải ổn
    # slot của từng box phải KHÔNG đổi dù config đảo thứ tự
    for b in (ba, bb, other):
        assert a3._slots[b.ssh] == s1[b.ssh]


def test_slots_stable_when_adding_removing_boxes():
    """Thêm box mới không đổi slot box cũ; bỏ box trả slot của nó."""
    b1, b2 = make_boxes("user@192.168.0.9", "user@192.168.0.10")
    adb = new_adb([b1, b2])
    p1_before = adb.port_for(b1, IP)

    b3 = Box(name="b3", ssh="user@192.168.0.11")
    adb.refresh_slots([b1, b2, b3])
    assert adb.port_for(b1, IP) == p1_before          # box cũ giữ nguyên port
    assert adb._slots[b3.ssh] not in (adb._slots[b1.ssh], adb._slots[b2.ssh])

    adb.refresh_slots([b1, b3])                        # bỏ box-2
    assert adb.port_for(b1, IP) == p1_before
    assert b2.ssh not in adb._slots                    # slot của box-2 đã trả


def test_ports_stay_in_box_range():
    """Port nằm đúng dải 256 của slot mình (không tràn sang dải box khác)."""
    boxes = make_boxes(*[f"user@192.168.0.{i}" for i in (9, 10, 11, 12)])
    adb = new_adb(boxes)
    for b in boxes:
        slot = adb._slots[b.ssh]
        for octet in (2, 50, 61, 250):
            p = adb.port_for(b, f"172.30.0.{octet}")
            assert PORT_BASE + slot * 256 <= p <= PORT_BASE + slot * 256 + 255


def test_purge_verified_only_touches_that_box():
    """reset_master của box A không xóa trạng thái verify của box B."""
    b1, b2 = make_boxes("user@192.168.0.9", "user@192.168.0.10")
    adb = new_adb([b1, b2])
    s1 = f"127.0.0.1:{adb.port_for(b1, IP)}"
    s2 = f"127.0.0.1:{adb.port_for(b2, IP)}"
    adb._verified = {s1, s2}
    adb._purge_verified(b1)
    assert s1 not in adb._verified
    assert s2 in adb._verified


def test_acquire_lock_single_instance(tmp_path):
    """Hai console trên cùng máy → cái sau từ chối chạy."""
    from console.cli import acquire_lock
    fd = acquire_lock(tmp_path)
    try:
        with pytest.raises(SystemExit, match="đã có farm-console"):
            acquire_lock(tmp_path)
    finally:
        import os
        os.close(fd)  # nhả lock
    fd2 = acquire_lock(tmp_path)  # sau khi thoát: chạy lại được
    import os
    os.close(fd2)
