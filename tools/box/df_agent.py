#!/usr/bin/env python3
"""df_agent.py — chạy trên box (root), quản lý máy Android cho farm-console.

Chỉ dùng thư viện chuẩn và docker CLI. farm-console đẩy file này qua stdin:
    ssh box "sudo -S -p '' python3 - <lệnh> [tham số]"
nên box không cần cài gì. Mọi lệnh in ra đúng 1 object JSON trên stdout
(lỗi: {"error": "..."} và exit 1).

Lệnh:
  info                         trạng thái host
  list                         danh sách container + trạng thái boot
  images                       image Android có trên box
  create NAME [--image I] [--width W --height H --dpi D --fps F] [--adbkey KEY]
              [--profile P]    đội lốt thiết bị P (tên hồ sơ | random | auto)
  start|stop|restart NAME
  rm NAME [--purge]            xoá container (--purge: xoá cả data)
  autostart NAME on|off        restart policy unless-stopped / no
  boot NAME                    {"running":..,"boot_completed":..,"ip":..}
  logs NAME [N]
  profiles                     danh sách hồ sơ thiết bị (device profile)

Hồ sơ thiết bị: mỗi máy đội lốt một Pixel Android 12 THẬT, nhất quán hoàn toàn
(fingerprint + model + build khớp nhau) nên GMS/Play nhận đúng và các máy không
trùng dấu vết. Áp bằng resetprop ngay lúc máy vừa boot (trước khi system_server
đọc Build.*), không cần Magisk/sửa image. Binary resetprop lấy từ RESETPROP_SRC.

Không có screencap/input ở đây: tiến trình `docker exec` mang context SELinux
u:r:kernel:s0, policy Android chặn nó gọi binder (SurfaceFlinger/InputManager)
nên lệnh treo. farm-console làm việc đó qua adbd (xem farm-console/console/adb.py).

Công thức container giống hệt vendor (đã kiểm trên box thật). Bắt buộc
`-v /dev/mali0:/dev/mali0`: driver Mali nhớ inode của /dev/mali0 ở lần mở đầu
tiên; `--device` cho mỗi container một node (inode) riêng, bị huỷ khi container
dừng, và container start sau đó làm kernel chết (box treo cứng). Bind node của
host thì mọi container dùng chung một inode. Xem HUONG-DAN.md mục 4.11.
"""

from __future__ import annotations

import glob
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

NETWORK = os.environ.get("DF_BRIDGE", "df-br")
DATA_ROOT = os.environ.get("DF_DATA_ROOT", "/mmc/devicefarm/data")
IP_START = int(os.environ.get("DF_IP_START", "50"))
# image đã kiểm chứng trên RK3588 (boot ~18s, GPU Mali); image khác chưa thử, có thể treo box
PROVEN_IMAGES = ("registry.cn-guangzhou.aliyuncs.com/mytos/dobox:Q12_gms_202510101644",)
#: image do golden_bake.py nướng: license-free (mọi slot, real-TEE, không bind runtime).
#: Ưu tiên dùng nếu có trên box — xem tools/box/GOLDEN-BAKE.md.
LICFREE_IMAGE = "dobox:Q12_licfree_1"
#: binary resetprop (applet của Magisk) đặt sẵn trên box; df_agent chép vào
#: /data mỗi máy để áp hồ sơ thiết bị. Lấy từ cache integrity: magisk -> resetprop.
RESETPROP_SRC = os.environ.get("DF_RESETPROP", "/opt/df/resetprop")
LABEL_MANAGED = "devicefarm.managed"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
# container của vendor MYTOS: <md5>_<n>_T<id> và myt_sdk
VENDOR_RE = re.compile(r"^([0-9a-f]{32}_[0-9]+_T[0-9]+|myt_sdk)$")

CAPS = ["SYSLOG", "AUDIT_CONTROL", "SETGID", "DAC_READ_SEARCH", "SYS_ADMIN", "NET_ADMIN",
        "SYS_MODULE", "SYS_NICE", "SYS_TIME", "SYS_TTY_CONFIG", "NET_BROADCAST", "IPC_LOCK",
        "SYS_RESOURCE", "SYS_PTRACE", "WAKE_ALARM", "BLOCK_SUSPEND", "MKNOD"]
DEVICES = ["tee0", "teepriv0", "crypto", "mali0", "rga", "dri", "mpp_service", "fuse",
           "input/event0", "ashmem", "dma_heap/cma", "dma_heap/cma-uncached", "dma_heap/system",
           "dma_heap/system-dma32", "dma_heap/system-uncached", "dma_heap/system-uncached-dma32"]
CGROUP_RULES = ["c 10:* rmw", "b 253:* rmw", "b 7:* rmw"]


class AgentError(Exception):
    pass


# --------------------------------------------------------------------------- device profiles
# Framework thật là Android 12 (API 31, SQ3A.220705.003.A1) — CHገ dùng fingerprint
# Android 12, dùng bản khác phiên bản sẽ lệch release/sdk. Pixel 4a–6a cùng nhận
# bản SQ3A.220705.003.A1 (7/2022) — đây là fingerprint THẬT của Google.
# Thêm hồ sơ: chép 1 dòng vào _PIXELS với model + codename thật (tra factory image).
import hashlib

_A12 = {"release": "12", "build_id": "SQ3A.220705.003.A1",
        "incremental": "8672226", "security_patch": "2022-07-05"}
_PIXELS = [
    ("pixel4a",   "Pixel 4a",      "sunfish"),
    ("pixel4a5g", "Pixel 4a (5G)", "bramble"),
    ("pixel5",    "Pixel 5",       "redfin"),
    ("pixel5a",   "Pixel 5a",      "barbet"),
    ("pixel6",    "Pixel 6",       "oriole"),
    ("pixel6pro", "Pixel 6 Pro",   "raven"),
    ("pixel6a",   "Pixel 6a",      "bluejay"),
]
PROFILES: Dict[str, Dict[str, str]] = {
    k: {"key": k, "brand": "google", "manufacturer": "Google",
        "model": m, "device": c, "name": c, **_A12}
    for k, m, c in _PIXELS
}
PROFILE_KEYS: List[str] = list(PROFILES)


def profile_fingerprint(p: Dict[str, str]) -> str:
    return (f'{p["brand"]}/{p["device"]}/{p["device"]}:{p["release"]}/'
            f'{p["build_id"]}/{p["incremental"]}:user/release-keys')


def choose_profile(name: str, requested: Optional[str]) -> str:
    """Hồ sơ cho máy `name`. requested: tên cụ thể, 'random', 'auto'/None.
    auto: chọn ổn định theo tên (băm) — trải đều pool, không đổi giữa các lần start."""
    if requested and requested not in ("auto", "random"):
        if requested not in PROFILES:
            raise AgentError(f"không có hồ sơ '{requested}' (xem: profiles)")
        return requested
    seed = os.urandom(8).hex() if requested == "random" else name
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return PROFILE_KEYS[h % len(PROFILE_KEYS)]


def gen_serial(name: str) -> str:
    """Serial giả ổn định theo tên máy (hex 16, hoa) — mỗi máy khác nhau."""
    return hashlib.sha256(("serial:" + name).encode()).hexdigest()[:16].upper()


def luhn_check_digit(body: str) -> str:
    """Check digit Luhn chuẩn IMEI (đầu vào 14 số, ra 1 số)."""
    total = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch)
        if i % 2 == 0:  # kể từ phải, vị trí chẵn được nhân đôi (check digit nằm bên phải cùng)
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def gen_imei(name: str) -> str:
    """IMEI 15 số ổn định theo tên máy, Luhn hợp lệ, TAC kiểu máy thật ('35...').
    Google checkin yêu cầu IMEI 15 chữ số — RIL giả của dobox không có, ta phải
    cung cấp qua prop gsm.sim.imei trước khi qcrild đọc (xem apply_profile)."""
    h = hashlib.sha256(("imei:" + name).encode()).hexdigest()
    body = "35" + "".join(str(int(c, 16) % 10) for c in h[:12])
    return body + luhn_check_digit(body)


def render_profile(key: str, serial: str) -> List[Tuple[str, str]]:
    """Danh sách (prop, value) để resetprop áp cho một máy."""
    p = PROFILES[key]
    fp = profile_fingerprint(p)
    out: List[Tuple[str, str]] = [
        (prop, fp) for prop in (
            "ro.build.fingerprint", "ro.bootimage.build.fingerprint",
            "ro.system.build.fingerprint", "ro.vendor.build.fingerprint",
            "ro.product.build.fingerprint", "ro.system_ext.build.fingerprint",
            "ro.odm.build.fingerprint", "ro.vendor_dlkm.build.fingerprint")
    ]
    out += [
        ("ro.build.id", p["build_id"]),
        ("ro.build.display.id", p["build_id"]),
        ("ro.build.version.incremental", p["incremental"]),
        ("ro.build.version.release", p["release"]),
        ("ro.build.version.security_patch", p["security_patch"]),
        ("ro.build.description",
         f'{p["device"]}-user {p["release"]} {p["build_id"]} {p["incremental"]} release-keys'),
        ("ro.build.flavor", f'{p["device"]}-user'),
        ("ro.build.product", p["device"]),
        ("ro.build.tags", "release-keys"),
        ("ro.build.type", "user"),
    ]
    # build.id/incremental từng partition — image gốc để lọt build Android 15
    # (AP3A.240905.015.A2/164800) mâu thuẫn với fingerprint Android 12 ở trên.
    for part in ("bootimage", "system", "system_ext", "vendor", "product",
                 "odm", "vendor_dlkm"):
        out.append((f"ro.{part}.build.id", p["build_id"]))
        out.append((f"ro.{part}.build.version.incremental", p["incremental"]))
    fields = {"brand": p["brand"], "manufacturer": p["manufacturer"],
              "model": p["model"], "name": p["name"], "device": p["device"]}
    for k, v in fields.items():
        out.append((f"ro.product.{k}", v))
        for part in ("system", "system_ext", "vendor", "product", "odm",
                     "bootimage", "vendor_dlkm"):
            out.append((f"ro.product.{part}.{k}", v))
    out += [("ro.product.board", p["device"])]
    out += [("ro.serialno", serial), ("ro.boot.serialno", serial)]
    return out


def profile_path(data: str) -> str:
    return os.path.join(data, "local", "df_profile.json")


def write_profile(data: str, key: str, serial: str, imei: Optional[str] = None) -> None:
    """Lưu hồ sơ đã chọn + đặt resetprop vào /data máy (tồn tại qua restart)."""
    tmp = os.path.join(data, "local", "tmp")
    os.makedirs(tmp, exist_ok=True)
    with open(profile_path(data), "w") as f:
        json.dump({"profile": key, "serial": serial,
                   "imei": imei or gen_imei(os.path.basename(data))}, f)
    dst = os.path.join(tmp, "resetprop")
    if os.path.exists(RESETPROP_SRC):
        shutil.copyfile(RESETPROP_SRC, dst)
        os.chmod(dst, 0o755)


def read_profile(data: str) -> Optional[Dict[str, str]]:
    try:
        with open(profile_path(data)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def apply_profile(name: str, data: str, wait: float = 60) -> Dict[str, Any]:
    """Áp hồ sơ bằng resetprop ngay khi property service sẵn sàng (trước boot xong).
    Không ném lỗi làm hỏng create/start — trả trạng thái để chẩn đoán."""
    prof = read_profile(data)
    if not prof:
        return {"applied": False, "reason": "không có hồ sơ"}
    rp = "/data/local/tmp/resetprop"
    if not os.path.exists(os.path.join(data, "local", "tmp", "resetprop")):
        return {"applied": False, "reason": f"thiếu resetprop (xem RESETPROP_SRC={RESETPROP_SRC})"}
    # chờ property service (getprop chạy được) — thường ~1–2s
    import time
    ok = False
    deadline = time.time() + wait
    while time.time() < deadline:
        r = subprocess.run(["docker", "exec", name, "/system/bin/getprop", "sys.boot_completed"],
                           capture_output=True)
        if r.returncode == 0:
            ok = True
            break
        time.sleep(1)
    if not ok:
        return {"applied": False, "reason": "property service không sẵn sàng"}
    pairs = render_profile(prof["profile"], prof["serial"])
    imei = prof.get("imei") or gen_imei(name)
    # một lần exec: nối tất cả lệnh resetprop (giá trị không chứa nháy đơn)
    script = "\n".join(f"{rp} '{p}' '{v}'" for p, v in pairs)
    # IMEI cho RIL giả (gsm.sim.imei) — phải có TRƯỚC khi qcrild phục vụ GET_IMEI;
    # Google checkin từ chối IMEI rỗng ("not a valid IMEI").
    script += f"\n{rp} gsm.sim.imei '{imei}'\n{rp} gsm.sim.imei1 '{imei}'"
    r = subprocess.run(["docker", "exec", name, "/system/bin/sh", "-c", script],
                       capture_output=True, timeout=60)
    # GMS cần READ_PHONE_STATE runtime để đọc IMEI cho checkin; /data tươi chưa
    # từng chạy setup-wizard nên quyền chưa được cấp. Chờ boot xong rồi cấp
    # (grant tồn tại trong /data, chỉ cần làm 1 lần — lặp lại vô hại).
    grant = ("i=0; while [ \"$(getprop sys.boot_completed)\" != \"1\" ] "
             "&& [ $i -lt 300 ]; do sleep 2; i=$((i+1)); done; "
             "pm grant com.google.android.gms android.permission.READ_PHONE_STATE 2>/dev/null; true")
    subprocess.run(["docker", "exec", "-d", name, "/system/bin/sh", "-c", grant],
                   capture_output=True, timeout=30)
    return {"applied": r.returncode == 0, "profile": prof["profile"],
            "serial": prof["serial"], "props": len(pairs) + 2, "imei": imei,
            "fingerprint": profile_fingerprint(PROFILES[prof["profile"]])}


# ----------------------------------------------------------------------------- helpers
def run(cmd: Sequence[str], timeout: float = 60, check: bool = True,
        binary: bool = False) -> Any:
    try:
        p = subprocess.run(list(cmd), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AgentError(f"quá thời gian: {' '.join(cmd[:4])}")
    if check and p.returncode != 0:
        err = p.stderr.decode(errors="replace").strip().splitlines()
        raise AgentError(err[-1] if err else f"{cmd[0]} exit {p.returncode}")
    return p.stdout if binary else p.stdout.decode(errors="replace")


def read(path: str, default: str = "") -> str:
    try:
        with open(path, errors="replace") as f:
            return f.read().strip().rstrip("\x00")
    except OSError:
        return default


def docker_json(*args: str) -> Any:
    out = run(["docker", *args])
    return json.loads(out) if out.strip() else []


def inspect_all() -> List[Dict[str, Any]]:
    ids = run(["docker", "ps", "-aq"]).split()
    return docker_json("inspect", *ids) if ids else []


def cname(c: Dict[str, Any]) -> str:
    return c["Name"].lstrip("/")


def binders_of(c: Dict[str, Any]) -> List[int]:
    out = []
    for d in c["HostConfig"].get("Devices") or []:
        m = re.fullmatch(r"/dev/binder(\d+)", d.get("PathOnHost", ""))
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def uses_mali(c: Dict[str, Any]) -> bool:
    if c["HostConfig"].get("Privileged"):
        return True
    return any(d.get("PathOnHost") == "/dev/mali0" for d in c["HostConfig"].get("Devices") or [])


def mali_bound(c: Dict[str, Any]) -> bool:
    return any(b.startswith("/dev/mali0:/dev/mali0") for b in c["HostConfig"].get("Binds") or [])


def ip_on_network(c: Dict[str, Any], net: str = NETWORK) -> Optional[str]:
    n = (c.get("NetworkSettings", {}).get("Networks") or {}).get(net)
    if not n:
        return None
    return n.get("IPAddress") or ((n.get("IPAMConfig") or {}).get("IPv4Address")) or None


def data_dir_of(c: Dict[str, Any]) -> Optional[str]:
    for b in c["HostConfig"].get("Binds") or []:
        parts = b.split(":")
        if len(parts) >= 2 and parts[1] == "/data":
            return parts[0]
    return None


def get_container(name: str) -> Dict[str, Any]:
    for c in inspect_all():
        if cname(c) == name:
            return c
    raise AgentError(f"không có container '{name}'")


# ----------------------------------------------------------------------------- allocation
def host_binders() -> List[int]:
    return sorted(int(m.group(1)) for p in glob.glob("/dev/binder*")
                  if (m := re.fullmatch(r"/dev/binder(\d+)", p)))


def pick_binders(available: Sequence[int], used: Sequence[int]) -> Tuple[int, int, int]:
    """Bộ ba binder (3k+1, 3k+2, 3k+3) đầu tiên còn đủ node và chưa ai dùng."""
    avail, taken = set(available), set(used)
    k = 0
    while 3 * k + 3 <= (max(avail) if avail else 0):
        trio = (3 * k + 1, 3 * k + 2, 3 * k + 3)
        if all(b in avail for b in trio) and not any(b in taken for b in trio):
            return trio
        k += 1
    raise AgentError("hết binder trống (mỗi máy cần 3 node /dev/binderN)")


def pick_ip(subnet: str, used: Sequence[str], start: int = IP_START) -> str:
    net = ipaddress.ip_network(subnet, strict=False)
    taken = set(used)
    hosts = list(net.hosts())
    for h in hosts[start - 1:]:
        if str(h) not in taken:
            return str(h)
    raise AgentError(f"hết IP trống trong {subnet}")


def network_subnet(net: str = NETWORK) -> str:
    try:
        info = docker_json("network", "inspect", net)
    except AgentError:
        raise AgentError(f"chưa có docker network '{net}' (chạy provision apply trước)")
    cfg = (info[0].get("IPAM") or {}).get("Config") or []
    if not cfg:
        raise AgentError(f"network '{net}' không có subnet")
    return cfg[0]["Subnet"]


def build_run_args(name: str, image: str, ip: str, trio: Sequence[int], data: str,
                   width: int, height: int, dpi: int, fps: int) -> List[str]:
    """Đối số `docker run` cho công thức vendor (thuần, không I/O — dễ test)."""
    a = ["docker", "run", "-d", "--name", name, "--hostname", name,
         "--label", f"{LABEL_MANAGED}=1", "--label", "devicefarm.recipe=vendor",
         "--network", NETWORK, "--ip", ip,
         "--security-opt", "seccomp=unconfined", "--cgroupns", "host"]
    for c in CAPS:
        a += ["--cap-add", c]
    for r in CGROUP_RULES:
        a += ["--device-cgroup-rule", r]
    for d in DEVICES:
        a += ["--device", f"/dev/{d}"]
    b1, b2, b3 = trio
    a += ["--device", f"/dev/binder{b1}:/dev/binder",
          "--device", f"/dev/binder{b2}:/dev/hwbinder",
          "--device", f"/dev/binder{b3}:/dev/vndbinder"]
    a += ["-v", "/dev/mali0:/dev/mali0",          # BẮT BUỘC, xem docstring
          "-v", f"{data}:/data", "-v", "/dev/net/tun:/dev/tun", "-v", "/proc/1/ns/net:/dev/mnetns"]
    a += [image, "androidboot.hardware=rk30board", "qemu=1",
          f"androidboot.dobox_width={width}", f"androidboot.dobox_height={height}",
          f"androidboot.dobox_dpi={dpi}", f"androidboot.dobox_fps={fps}",
          "androidboot.dobox_net_ndns=1", "androidboot.dobox_net_dns1=8.8.8.8"]
    return a


def unsafe_gpu_containers(containers: List[Dict[str, Any]], exclude: str = "") -> List[str]:
    """Container dùng GPU mà không bind /dev/mali0 của host (nguy cơ treo box)."""
    return [cname(c) for c in containers
            if cname(c) != exclude and uses_mali(c) and not mali_bound(c)]


# ----------------------------------------------------------------------------- commands
def boot_state(name: str, running: bool) -> Optional[bool]:
    if not running:
        return None
    try:
        out = run(["docker", "exec", name, "/system/bin/getprop", "sys.boot_completed"],
                  timeout=5, check=False)
    except AgentError:
        return False
    return out.strip() == "1"


def cmd_info(_: List[str]) -> Dict[str, Any]:
    uptime = float(read("/proc/uptime", "0").split()[0] or 0)
    mem = {}
    for line in read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = int(v.split()[0]) // 1024
    temps = []
    for z in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        t = read(z)
        if t.lstrip("-").isdigit():
            temps.append(int(t) / 1000)

    def disk(p: str) -> Optional[Dict[str, int]]:
        try:
            u = shutil.disk_usage(p)
        except OSError:
            return None
        return {"total_gb": u.total // 2**30, "free_gb": u.free // 2**30}

    gpu = "/sys/class/devfreq/fb000000.gpu"
    dmesg = run(["dmesg"], check=False)
    fw = ("ok" if "Loading Mali firmware" in dmesg else
          "failed" if "Firmware initialization failed" in dmesg else "unknown")

    def svc(name: str) -> str:
        r = subprocess.run(["rc-service", name, "status"], capture_output=True)
        return "running" if r.returncode == 0 else "stopped"

    docker_ok = subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    vendor = svc("mytsvr") if os.path.exists("/etc/init.d/mytsvr") else "absent"
    return {
        "hostname": read("/proc/sys/kernel/hostname"),
        "model": read("/proc/device-tree/model"),
        "compatible": read("/proc/device-tree/compatible").replace("\x00", " "),
        "kernel": run(["uname", "-r"]).strip() + " " + run(["uname", "-v"]).strip().split()[0],
        "uptime_s": int(uptime),
        "load": [float(x) for x in read("/proc/loadavg", "0 0 0").split()[:3]],
        "cpus": os.cpu_count(),
        "mem_mb": mem,
        "disk": {"root": disk("/"), "data": disk("/mmc")},
        "temp_c": max(temps) if temps else None,
        "gpu": {"governor": read(f"{gpu}/governor"), "freq_mhz": int(read(f"{gpu}/cur_freq", "0")) // 10**6,
                "load": read(f"{gpu}/load"), "firmware": fw},
        "selinux_enforce": read("/sys/fs/selinux/enforce"),
        "tee_supplicant": bool(run(["pidof", "tee-supplicant"], check=False).strip()),
        "vendor_mytsvr": vendor,
        "docker": docker_ok,
        "binders": len(host_binders()),
    }


def cmd_list(_: List[str]) -> Dict[str, Any]:
    cs = inspect_all()
    out = []
    for c in cs:
        name, running = cname(c), c["State"].get("Running", False)
        out.append({
            "name": name,
            "id": c["Id"][:12],
            "image": c["Config"].get("Image"),
            "state": c["State"].get("Status"),
            "started_at": c["State"].get("StartedAt"),
            "restart": (c["HostConfig"].get("RestartPolicy") or {}).get("Name") or "no",
            "ip": ip_on_network(c),
            "binders": binders_of(c),
            "gpu": uses_mali(c),
            "mali_bind": mali_bound(c),
            "privileged": bool(c["HostConfig"].get("Privileged")),
            "managed": (c["Config"].get("Labels") or {}).get(LABEL_MANAGED) == "1",
            "vendor": bool(VENDOR_RE.match(name)),
            "data": data_dir_of(c),
            "boot_completed": boot_state(name, running) if uses_mali(c) else None,
        })
    out.sort(key=lambda d: (d["state"] != "running", d["name"]))
    return {"devices": out, "unsafe": unsafe_gpu_containers(cs)}


def cmd_images(_: List[str]) -> Dict[str, Any]:
    out = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}"])
    imgs = []
    for line in out.splitlines():
        ref, size, since = (line.split("\t") + ["", ""])[:3]
        if "dobox" in ref and "sdk" not in ref:
            imgs.append({"ref": ref, "size": size, "created": since, "proven": ref in PROVEN_IMAGES})
    imgs.sort(key=lambda i: not i["proven"])
    return {"images": imgs}


def opt(args: List[str], key: str, default: Optional[str] = None) -> Optional[str]:
    if key in args:
        i = args.index(key)
        if i + 1 >= len(args):
            raise AgentError(f"thiếu giá trị cho {key}")
        return args[i + 1]
    return default


def cmd_create(args: List[str]) -> Dict[str, Any]:
    if not args:
        raise AgentError("thiếu tên máy")
    name = args[0]
    if not NAME_RE.match(name):
        raise AgentError("tên máy chỉ gồm a-z 0-9 _ - (tối đa 31 ký tự)")
    cs = inspect_all()
    if any(cname(c) == name for c in cs):
        raise AgentError(f"đã có container '{name}'")
    unsafe = unsafe_gpu_containers(cs)
    if unsafe:
        raise AgentError("có container dùng GPU mà không bind /dev/mali0 (dừng/start sẽ treo box): "
                         + ", ".join(unsafe) + ". Xoá chúng trước.")
    if not run(["pidof", "tee-supplicant"], check=False).strip():
        raise AgentError("tee-supplicant không chạy (Android sẽ kẹt khi boot) — chạy provision apply")
    if not os.path.exists("/dev/mali0"):
        raise AgentError("box không có /dev/mali0")
    image = opt(args, "--image")
    if not image:
        imgs = cmd_images([])["images"]
        if not imgs:
            raise AgentError("box chưa có image Android (dobox)")
        # ưu tiên image license-free đã nướng (chạy được mọi slot với real-TEE)
        licfree = [i["ref"] for i in imgs if i["ref"] == LICFREE_IMAGE]
        image = (licfree or imgs)[0]["ref"]
    width = int(opt(args, "--width", "720") or 720)
    height = int(opt(args, "--height", "1280") or 1280)
    dpi = int(opt(args, "--dpi", "320") or 320)
    fps = int(opt(args, "--fps", "24") or 24)

    used_b = [b for c in cs for b in binders_of(c)]
    trio = pick_binders(host_binders(), used_b)
    used_ip = [ip for c in cs if (ip := ip_on_network(c))]
    ip = pick_ip(network_subnet(), used_ip)

    data = os.path.join(DATA_ROOT, name)
    adb_dir = os.path.join(data, "misc", "adb")
    os.makedirs(adb_dir, exist_ok=True)
    key = opt(args, "--adbkey")
    if key:
        kf = os.path.join(adb_dir, "adb_keys")
        old = read(kf)
        if key.strip() not in old:
            with open(kf, "a") as f:
                f.write(key.strip() + "\n")
        for p in (os.path.join(data, "misc"), adb_dir, kf):
            os.chown(p, 1000, 2000)
        os.chmod(adb_dir, 0o2750)
        os.chmod(kf, 0o640)

    # hồ sơ thiết bị: chọn + ghi vào /data TRƯỚC khi boot để áp sớm
    prof_key = choose_profile(name, opt(args, "--profile", "auto"))
    serial = gen_serial(name)
    write_profile(data, prof_key, serial, gen_imei(name))

    cid = run(build_run_args(name, image, ip, trio, data, width, height, dpi, fps), timeout=120).strip()
    prof_res = apply_profile(name, data)
    return {"name": name, "id": cid[:12], "ip": ip, "binders": list(trio), "image": image,
            "data": data, "profile": prof_res}


def need_name(args: List[str]) -> str:
    if not args:
        raise AgentError("thiếu tên máy")
    return args[0]


def cmd_simple(action: str) -> Any:
    def f(args: List[str]) -> Dict[str, Any]:
        name = need_name(args)
        c = get_container(name)
        if action in ("start", "restart"):
            others = unsafe_gpu_containers(inspect_all(), exclude="")
            if others:
                raise AgentError("có container dùng GPU mà không bind /dev/mali0: " + ", ".join(others)
                                 + ". Start lúc này có thể treo box — xoá/tạo lại chúng trước.")
        run(["docker", action, cname(c)], timeout=90)
        res: Dict[str, Any] = {"name": name, "action": action}
        # sau start/restart: áp lại hồ sơ thiết bị sớm (trước khi system_server đọc Build.*)
        if action in ("start", "restart"):
            data = data_dir_of(c)
            if data and read_profile(data):
                res["profile"] = apply_profile(name, data)
        return res
    return f


def cmd_setprofile(args: List[str]) -> Dict[str, Any]:
    """Gán/đổi hồ sơ thiết bị cho máy đã tạo. --restart: boot lại để áp sớm
    (khuyến nghị — không restart thì các process đang chạy vẫn giữ Build.* cũ)."""
    name = need_name(args)
    c = get_container(name)
    data = data_dir_of(c)
    if not data:
        raise AgentError("không tìm thấy thư mục /data của máy")
    prof_key = choose_profile(name, opt(args, "--profile", "auto"))
    serial = gen_serial(name)
    prof = read_profile(data) or {}
    write_profile(data, prof_key, serial, prof.get("imei") or gen_imei(name))
    if "--restart" in args:
        run(["docker", "restart", name], timeout=90)
        res = apply_profile(name, data)
    else:
        res = apply_profile(name, data, wait=5)
        res["note"] = "chưa restart: process cũ vẫn giữ Build.* — restart để áp đầy đủ"
    return {"name": name, "assigned": prof_key, "restarted": "--restart" in args, "apply": res}


def cmd_profiles(_: List[str]) -> Dict[str, Any]:
    return {"profiles": [
        {"key": k, "model": PROFILES[k]["model"], "device": PROFILES[k]["device"],
         "fingerprint": profile_fingerprint(PROFILES[k])}
        for k in PROFILE_KEYS]}


def cmd_rm(args: List[str]) -> Dict[str, Any]:
    name = need_name(args)
    c = get_container(name)
    data = data_dir_of(c)
    run(["docker", "rm", "-f", name], timeout=90)
    purged = False
    if "--purge" in args and data:
        p = os.path.normpath(data)
        if not p.startswith(DATA_ROOT.rstrip("/") + "/") or p.count("/") < 4:
            raise AgentError(f"không xoá data ngoài {DATA_ROOT}: {p}")
        shutil.rmtree(p, ignore_errors=True)
        purged = True
    return {"name": name, "removed": True, "purged": purged}


def cmd_autostart(args: List[str]) -> Dict[str, Any]:
    name = need_name(args)
    on = (args[1:2] or ["on"])[0] == "on"
    get_container(name)
    run(["docker", "update", "--restart", "unless-stopped" if on else "no", name])
    return {"name": name, "autostart": on}


def cmd_boot(args: List[str]) -> Dict[str, Any]:
    name = need_name(args)
    c = get_container(name)
    running = c["State"].get("Running", False)
    return {"name": name, "running": running, "boot_completed": bool(boot_state(name, running)),
            "ip": ip_on_network(c),
            "load": [float(x) for x in read("/proc/loadavg", "0 0 0").split()[:3]]}


def cmd_logs(args: List[str]) -> Dict[str, Any]:
    name = need_name(args)
    n = (args[1:2] or ["100"])[0]
    p = subprocess.run(["docker", "logs", "--tail", str(int(n)), name], capture_output=True, timeout=20)
    return {"name": name, "logs": (p.stdout + p.stderr).decode(errors="replace")}


# ------------------------------------------------- keybox nhúng (chain DEVICE)
# Cơ chế ĐÃ CHỨNG MINH 2026-11-06 (xem GOLDEN-BAKE.md + NOTES.md): chuỗi keybox
# được serve từ libsoft_attestation_cert.so NHÚNG trong super.img của image —
# HAL không bao giờ đọc /data/system/keymaster.xml. Xoay vòng = thay data lib
# trong layer của image rồi TẠO LẠI máy (docker restart làm mất netns wiring).
# Offset golden-layout: vendor lib @0x82254000 (= seek 533076 × 4096, tính bằng python!).
CHAIN_LIB_OFFSET = 0x82254000
CHAIN_LIB_SIZE = 1183168


def _image_super_path(image_ref: str) -> str:
    """super.img của image — layer MỚI NHẤT có nó (bake layer đè base)."""
    out = run(["docker", "inspect", image_ref, "--format",
               "{{.GraphDriver.Data.LowerDir}}"], check=False).strip()
    for d in [x for x in out.split(":") if x]:   # image: layer mới nhất đứng trước
        p = os.path.join(d, "super.img")
        if os.path.exists(p):
            return p
    raise AgentError(f"không tìm super.img trong layer của image {image_ref}")


def cmd_image_lib(args: List[str]) -> Dict[str, Any]:
    """image-lib <image> — đọc lib chain trong super của image → md5 + base64."""
    if not args:
        raise AgentError("thiếu tên image")
    image = args[0]
    p = _image_super_path(image)
    with open(p, "rb") as f:
        f.seek(CHAIN_LIB_OFFSET)
        blob = f.read(CHAIN_LIB_SIZE)
    if len(blob) != CHAIN_LIB_SIZE:
        raise AgentError(f"super.img của {image} đọc không đủ {CHAIN_LIB_SIZE}B (thấy {len(blob)}B)")
    import base64
    import hashlib
    return {"image": image, "super": p, "md5": hashlib.md5(blob).hexdigest(),
            "b64": base64.b64encode(blob).decode()}


def cmd_patch_image_lib(args: List[str]) -> Dict[str, Any]:
    """patch-image-lib <image> <file> — ghi đè lib chain trong layer của image."""
    if len(args) < 2:
        raise AgentError("cần: <image> <file-lib-đã-patch>")
    image, src = args[0], args[1]
    with open(src, "rb") as f:
        blob = f.read()
    if len(blob) != CHAIN_LIB_SIZE:
        raise AgentError(f"lib phải đúng {CHAIN_LIB_SIZE}B (thấy {len(blob)}B)")
    if blob[:4] != b"\x7fELF":
        raise AgentError("file không phải ELF — lib hỏng?")
    p = _image_super_path(image)
    with open(p, "r+b") as f:
        f.seek(CHAIN_LIB_OFFSET)
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
        f.seek(CHAIN_LIB_OFFSET)
        back = f.read(CHAIN_LIB_SIZE)
    import hashlib
    md5 = hashlib.md5(back).hexdigest()
    if back != blob:
        raise AgentError(f"ghi xong đọc lại không khớp (md5 {md5})")
    return {"image": image, "super": p, "md5": md5}


def cmd_dev_info(args: List[str]) -> Dict[str, Any]:
    """dev-info <máy> — image/hồ sơ/kích thước hiện tại (xoay keybox cần tạo lại nguyên vẹn)."""
    name = need_name(args)
    c = next((c for c in inspect_all() if cname(c) == name), None)
    if c is None:
        raise AgentError(f"không có container '{name}'")
    info: Dict[str, Any] = {"name": name, "image": c["Config"].get("Image"),
                            "data": data_dir_of(c)}
    prof = {}
    if info["data"]:
        pp = os.path.join(info["data"], "local", "df_profile.json")
        try:
            prof = json.loads(open(pp).read())
        except Exception:
            prof = {}
    info["profile"] = prof.get("profile")
    info["serial"] = prof.get("serial")
    info["imei"] = prof.get("imei")
    cmd_txt = " ".join(c["Config"].get("Cmd") or [])
    for k in ("width", "height", "dpi", "fps"):
        m = re.search(rf"dobox_{k}=(\d+)", cmd_txt)
        info[k] = int(m.group(1)) if m else None
    return info


def cmd_adbkey_add(args: List[str]) -> Dict[str, Any]:
    """adbkey-add <máy-hoặc-IP> <nội-dung-pubkey> — cấp adb key cho máy ĐÃ tạo.

    Máy tạo từ laptop A chỉ authorize key của A; laptop B (đồng nghiệp) cắm vào
    sẽ bị unauthorized. Console tự gọi lệnh này rồi connect lại — không cần tạo lại máy.
    """
    if len(args) < 2 or not args[1].strip():
        raise AgentError("cần: <máy-hoặc-IP> <nội-dung-pubkey-adb>")
    target, key = args[0], args[1].strip()
    if "." in target:                       # tra theo IP nếu tham số có dấu chấm
        c = next((c for c in inspect_all() if ip_on_network(c) == target), None)
        if c is None:
            raise AgentError(f"không có máy nào có IP {target}")
        name = cname(c)
    else:
        name = target
        c = get_container(name)
    data = data_dir_of(c)
    if not data:
        raise AgentError(f"không tìm /data của '{name}'")
    adb_dir = os.path.join(data, "misc", "adb")
    kf = os.path.join(adb_dir, "adb_keys")
    os.makedirs(adb_dir, exist_ok=True)
    old = read(kf)
    if key not in old:
        with open(kf, "a") as f:
            f.write(key + "\n")
    for p in (os.path.join(data, "misc"), adb_dir, kf):   # đúng ownership như cmd_create
        os.chown(p, 1000, 2000)
    os.chmod(adb_dir, 0o2750)
    os.chmod(kf, 0o640)
    return {"name": name, "added": key not in old}


COMMANDS = {
    "info": cmd_info, "list": cmd_list, "images": cmd_images, "create": cmd_create,
    "start": cmd_simple("start"), "stop": cmd_simple("stop"), "restart": cmd_simple("restart"),
    "rm": cmd_rm, "autostart": cmd_autostart, "boot": cmd_boot, "logs": cmd_logs,
    "profiles": cmd_profiles, "setprofile": cmd_setprofile,
    "dev-info": cmd_dev_info, "image-lib": cmd_image_lib, "patch-image-lib": cmd_patch_image_lib,
    "adbkey-add": cmd_adbkey_add,
}


def main(argv: List[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(json.dumps({"error": "lệnh: " + " | ".join(COMMANDS)}))
        return 2
    try:
        res = COMMANDS[argv[0]](argv[1:])
    except AgentError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 1
    except Exception as e:  # lỗi không lường trước: vẫn trả JSON cho console
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1
    print(res if isinstance(res, str) else json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
