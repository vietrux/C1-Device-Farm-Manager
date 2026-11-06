"""``farm-console`` — mở web console như một app: bật khi cần, đóng tab là tự tắt.

Mặc định: cổng ngẫu nhiên do hệ điều hành cấp (không bao giờ trùng service khác),
token ngẫu nhiên cho mỗi lần chạy, tự mở trình duyệt, tự thoát khi không còn tab
nào mở (và không còn tác vụ đang chạy).
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import List, Optional

from . import __version__
from .app import create_app
from .remote import Remote
from .store import BoxStore

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = Path(os.environ.get("FARM_CONSOLE_HOME", Path.home() / ".config" / "farm-console"))
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

log = logging.getLogger("console")


def bind_socket(host: str, port: int) -> socket.socket:
    """Mở sẵn socket nghe. port=0: hệ điều hành chọn một cổng đang trống.

    Socket được giao thẳng cho uvicorn nên không có khoảng hở giữa lúc chọn cổng
    và lúc nghe (không thể bị service khác chiếm mất).
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def cleanup_ssh(control_dir: Path) -> None:
    """Đóng các kết nối SSH master (kèm port forward adb) khi console tắt."""
    for s in control_dir.glob("*"):
        if s.is_socket():
            subprocess.run(["ssh", "-S", str(s), "-O", "exit", "farm-console"],
                           capture_output=True, timeout=10)


def adb_disconnect_local() -> None:
    """Ngắt mọi serial adb 127.0.0.1:* còn treo từ lần chạy trước.

    Forward của serial đó nằm trên SSH master cũ (đã bị cleanup_ssh đóng) nên
    để lại trong adb server chỉ là "cánh cửa khóa" — ngắt đi cho sạch.
    """
    if not shutil.which("adb"):
        return
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                         timeout=15).stdout
    for line in out.splitlines():
        if line.startswith("127.0.0.1:"):
            subprocess.run(["adb", "disconnect", line.split()[0]],
                           capture_output=True, timeout=10)


def acquire_lock(config_dir: Path) -> int:
    """Chỉ MỘT console mỗi máy — hai cụm cùng chạy sẽ giành port tunnel/adb.

    Trả fd của lockfile (giữ mở tới khi thoát, tự nhả kể cả khi crash).
    Ném SystemExit nếu đã có console khác đang chạy.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(config_dir / "console.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.lseek(fd, 0, os.SEEK_SET)
        old = os.read(fd, 32).decode(errors="replace").strip()
        os.close(fd)
        raise SystemExit(
            f"LỖI: đã có farm-console khác đang chạy (pid {old or '?'}). "
            "Hai console cùng máy sẽ trùng port tunnel/adb — tắt cái cũ trước.")
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="farm-console", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1",
                   help="địa chỉ lắng nghe (mặc định 127.0.0.1 = chỉ máy này)")
    p.add_argument("--port", type=int, default=0, help="cổng; 0 (mặc định) = ngẫu nhiên, còn trống")
    p.add_argument("--no-browser", action="store_true", help="không tự mở trình duyệt")
    p.add_argument("--idle-exit", type=int, default=180, metavar="GIÂY",
                   help="tự tắt khi không còn tab nào mở sau chừng này giây (0 = chạy mãi)")
    p.add_argument("--token", default=os.environ.get("FARM_CONSOLE_TOKEN"),
                   help="token cố định (mặc định: sinh ngẫu nhiên mỗi lần chạy)")
    p.add_argument("--no-token", action="store_true", help="tắt token (chỉ nên dùng khi debug)")
    p.add_argument("--boxes", type=Path, default=CONFIG_DIR / "boxes.yaml",
                   help="file danh sách box (quyền 0600)")
    p.add_argument("--scripts", type=Path, default=REPO_ROOT / "tools" / "box",
                   help="thư mục chứa df_agent.py và df-host.sh")
    p.add_argument("--adbkey", type=Path, default=Path.home() / ".android" / "adbkey.pub",
                   help="public key adb cài vào máy mới tạo")
    p.add_argument("--keyboxes", type=Path,
                   default=Path(os.environ.get("FARM_KEYBOX_DIR", CONFIG_DIR / "keyboxes")),
                   help="thư mục chứa keybox *.xml cho integrity-fix")
    p.add_argument("--integrity-assets", type=Path,
                   default=Path(os.environ.get("FARM_INTEGRITY_ASSETS",
                                Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                                / "devicefarm" / "integrity")),
                   help="cache Magisk/PIF (do tools/integrity/fetch-assets.sh tạo)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for f in ("df_agent.py", "df-host.sh"):
        if not (a.scripts / f).is_file():
            print(f"LỖI: thiếu {a.scripts / f} (chỉ đúng --scripts)", file=sys.stderr)
            return 2
    token = None if a.no_token else (a.token or secrets.token_urlsafe(18))
    if a.host not in LOCAL_HOSTS and not token:
        print("LỖI: mở ra mạng thì phải có token (bỏ --no-token).", file=sys.stderr)
        return 2

    try:
        sock = bind_socket(a.host, a.port)
    except OSError as e:
        print(f"LỖI: không nghe được {a.host}:{a.port}: {e}", file=sys.stderr)
        return 2
    port = sock.getsockname()[1]

    import uvicorn

    lock_fd = acquire_lock(CONFIG_DIR)  # một console mỗi máy, giữ fd tới khi thoát

    # Dọn tàn dư lần chạy trước: SSH master cũ (với forward 127.0.0.1:17xxx còn
    # sống nhờ ControlPersist) + serial adb treo. Không dọn thì port trùng/khớp
    # sai sau khi đổi boxes.yaml — nguồn gốc lỗi "tunnel trùng nhau ≥2 box".
    control_dir = CONFIG_DIR / "ssh"
    control_dir.mkdir(parents=True, exist_ok=True)
    cleanup_ssh(control_dir)
    adb_disconnect_local()

    store = BoxStore.load(a.boxes)
    remote = Remote(a.scripts, control_dir)
    app = create_app(store, remote, token=token, adbkey_path=a.adbkey,
                     android_sh=REPO_ROOT / "tools" / "android.sh",
                     keybox_dir=a.keyboxes, assets_dir=a.integrity_assets)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

    shown_host = "localhost" if a.host in LOCAL_HOSTS else a.host
    url = f"http://{shown_host}:{port}/" + (f"?token={token}" if token else "")

    def after_start() -> None:
        while not server.started and not server.should_exit:
            time.sleep(0.1)
        if server.should_exit:
            return
        print(f"farm-console {__version__} đang chạy: {url}", flush=True)
        print(f"  {len(store.boxes)} box ({a.boxes}). Tắt: Ctrl+C"
              + (f", hoặc đóng tab — tự tắt sau {a.idle_exit}s không dùng." if a.idle_exit else "."),
              flush=True)
        if not a.no_browser:
            webbrowser.open(url)

    def idle_watch() -> None:
        while not server.should_exit:
            time.sleep(5)
            idle = time.monotonic() - app.state.last_activity
            if idle > a.idle_exit and app.state.jobs.running() == 0:
                print(f"Không còn tab nào mở trong {a.idle_exit}s, tắt console.", flush=True)
                server.should_exit = True

    threading.Thread(target=after_start, daemon=True).start()
    if a.idle_exit:
        threading.Thread(target=idle_watch, daemon=True).start()
    try:
        server.run(sockets=[sock])
    finally:
        cleanup_ssh(control_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
