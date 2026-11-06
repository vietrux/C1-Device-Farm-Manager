"""FastAPI app: JSON API + trang web một file (static/index.html)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .adb import Adb
from .integrity import Integrity, IntegrityError
from .proxy import Proxy, ProxyError
from .jobs import Job, JobManager
from .keybox import KeyboxPool
from .remote import Remote, RemoteError
from .store import Box, BoxStore, StoreError

log = logging.getLogger(__name__)

# giao diện Next.js + shadcn/ui, build tĩnh: cd web && npm ci && npm run build  ->  web/out
WEB_DIR = Path(os.environ.get("FARM_CONSOLE_WEB", Path(__file__).resolve().parent.parent / "web" / "out"))
BOOT_TIMEOUT_S = 180
POLL_S = 3
# Chống "box melting" khi boot: KHÔNG giết nhầm lúc boot spike. Chỉ dừng khi load
# 1-phút vượt trần LIÊN TỤC nhiều lần. Trần = max(sàn tuyệt đối, hệ số × số core)
# — boot nhiều Android một lúc làm load lên ~0.6×core, chỉ melting thật mới vượt 3×core.
LOAD_GUARD = float(os.environ.get("FARM_CONSOLE_LOAD_GUARD", "12"))          # sàn tuyệt đối
LOAD_PER_CORE = float(os.environ.get("FARM_CONSOLE_LOAD_PER_CORE", "3"))     # trần theo core
LOAD_GUARD_STRIKES = int(os.environ.get("FARM_CONSOLE_LOAD_STRIKES", "3"))   # lần cao liên tiếp
# Batch: chờ tải hạ dưới ngưỡng này (mặc định = số core) trước khi tạo máy kế → không dồn tải.
SETTLE_MAX_S = int(os.environ.get("FARM_CONSOLE_SETTLE_MAX_S", "180"))
SETTLE_POLL_S = 5


def _load_ceiling(cpus: Optional[int]) -> float:
    return max(LOAD_GUARD, LOAD_PER_CORE * (cpus or 4))


def _load1(st: Any) -> float:
    try:
        return float((st.get("load") or [0])[0])
    except (TypeError, ValueError, IndexError):
        return 0.0
PROVISION_MODES = {"check": ["check"], "apply": ["apply", "--takeover"],
                   "takeover": ["apply", "--takeover"],   # bí danh cũ của apply
                   "revert": ["revert"]}


class BoxIn(BaseModel):
    name: str
    ssh: str
    sudo_password: Optional[str] = None
    note: str = ""


class CopyIdIn(BaseModel):
    ssh_password: str


class DeviceIn(BaseModel):
    name: str
    image: Optional[str] = None
    width: int = Field(720, ge=240, le=2160)
    height: int = Field(1280, ge=320, le=3840)
    dpi: int = Field(320, ge=120, le=640)
    fps: int = Field(24, ge=10, le=60)
    autostart: bool = True
    profile: Optional[str] = None        # hồ sơ thiết bị (tên | auto | random); None=auto
    count: int = Field(1, ge=1, le=12)   # >1: tạo hàng loạt, tên = <name>-<i>


class ProxyIn(BaseModel):
    host: str
    port: int
    kind: str = "socks5"   # socks5 | http
    user: str = ""
    password: str = ""


class IntegrityIn(BaseModel):
    keybox: Optional[str] = None   # pool name; None = best available, "none" = no keybox


class InputIn(BaseModel):
    type: str
    x: Optional[int] = None
    y: Optional[int] = None
    x2: Optional[int] = None
    y2: Optional[int] = None
    ms: Optional[int] = None
    key: Optional[str] = None
    text: Optional[str] = None


def create_app(store: BoxStore, remote: Remote, token: Optional[str] = None,
               adbkey_path: Optional[Path] = None, android_sh: Optional[Path] = None,
               adb: Optional[Adb] = None, keybox_dir: Optional[Path] = None,
               assets_dir: Optional[Path] = None) -> FastAPI:
    app = FastAPI(title="farm-console", version=__version__)
    jobs = JobManager()
    # lần cuối trình duyệt gọi API: cli dùng để tự tắt console khi không còn tab nào mở
    app.state.last_activity = time.monotonic()

    @app.middleware("http")
    async def touch(request: Request, call_next: Any) -> Any:
        if request.url.path.startswith("/api/"):
            app.state.last_activity = time.monotonic()
        return await call_next(request)
    def _pub() -> Optional[str]:
        p = adbkey_path or Path.home() / ".android" / "adbkey.pub"
        try:
            return p.read_text().strip() or None
        except OSError:
            return None

    adb = adb or Adb(remote, key_pub=_pub())  # type: ignore[arg-type]
    adb.refresh_slots(store.boxes)  # slot port cố định theo config, dùng cho mọi box
    keyboxes = KeyboxPool(keybox_dir)
    integrity = Integrity(adb, keyboxes, remote=remote, assets_dir=assets_dir)
    proxy = Proxy(adb)
    ips: Dict[tuple, str] = {}   # (box, máy) -> IP, để mỗi khung hình không phải hỏi lại box

    async def device_ip(box: Box, dev: str) -> str:
        ip = ips.get((box.name, dev))
        if not ip:
            st = await agent(box, "boot", dev, timeout=30)
            if not st.get("running"):
                raise HTTPException(409, f"máy {dev} không chạy")
            ip = st.get("ip")
            if not ip:
                raise HTTPException(409, f"máy {dev} không có IP trên mạng farm")
            ips[(box.name, dev)] = ip
        return ip

    async def via_adb(box: Box, dev: str, fn: Any, *args: Any) -> Any:
        try:
            return await fn(box, await device_ip(box, dev), *args)
        except RemoteError as e:
            ips.pop((box.name, dev), None)
            raise HTTPException(502, str(e))

    # ------------------------------------------------------------------ auth
    def auth(request: Request) -> None:
        if not token:
            return
        got = (request.headers.get("x-token") or request.query_params.get("token") or "")
        if not secrets.compare_digest(got, token):
            raise HTTPException(401, "cần token (mở trang với ?token=...)")

    def box_or_404(name: str) -> Box:
        try:
            return store.get(name)
        except KeyError:
            raise HTTPException(404, f"không có box '{name}'")

    async def agent(box: Box, *args: str, timeout: float = 60, raw: bool = False) -> Any:
        try:
            return await remote.agent(box, *args, timeout=timeout, raw=raw)
        except RemoteError as e:
            raise HTTPException(502, str(e))

    def adbkey() -> Optional[str]:
        p = adbkey_path or Path.home() / ".android" / "adbkey.pub"
        try:
            return p.read_text().strip() or None
        except OSError:
            return None

    # ------------------------------------------------------------------ pages
    @app.get("/api/ping", dependencies=[Depends(auth)])
    def ping() -> Dict[str, Any]:
        """Trình duyệt gọi định kỳ (kể cả khi tab ẩn) để console biết vẫn còn người dùng."""
        return {"ok": True}

    @app.get("/api/meta", dependencies=[Depends(auth)])
    def meta() -> Dict[str, Any]:
        return {"version": __version__, "adbkey": bool(adbkey()), "adb": adb.available, "sshpass": bool(shutil.which("sshpass")),
                "scrcpy": bool(android_sh and android_sh.exists() and shutil.which("scrcpy")),
                "load_guard": LOAD_GUARD}

    # ------------------------------------------------------------------ boxes
    @app.get("/api/boxes", dependencies=[Depends(auth)])
    def list_boxes() -> List[Dict[str, Any]]:
        out = []
        for b in store.boxes:
            d = b.public()
            d["main_image"] = b.main_image or store.default_main_image
            out.append(d)
        return out

    @app.post("/api/boxes", dependencies=[Depends(auth)])
    async def add_box(body: BoxIn) -> Dict[str, Any]:
        try:
            box = store.add(body.name.strip(), body.ssh.strip(), body.sudo_password, body.note)
        except StoreError as e:
            raise HTTPException(400, str(e))
        adb.refresh_slots(store.boxes)  # box mới nhận slot ngay, không đụng slot cũ
        return {**box.public(), "ssh_error": await remote.check_ssh(box)}

    @app.delete("/api/boxes/{name}", dependencies=[Depends(auth)])
    def remove_box(name: str) -> Dict[str, Any]:
        try:
            store.remove(name)
        except KeyError:
            raise HTTPException(404, f"không có box '{name}'")
        adb.refresh_slots(store.boxes)  # trả slot của box đã bỏ
        return {"removed": name}

    @app.post("/api/boxes/{name}/copy-id", dependencies=[Depends(auth)])
    async def copy_id(name: str, body: CopyIdIn) -> Dict[str, Any]:
        """Cài SSH key của máy chạy console lên box (box mới mua chỉ có mật khẩu)."""
        box = box_or_404(name)
        if not shutil.which("sshpass"):
            raise HTTPException(400, "máy chạy console chưa có sshpass (apt install sshpass), "
                                     f"hoặc chạy tay: ssh-copy-id {box.ssh}")

        async def run(job: Job) -> Any:
            env = {**os.environ, "SSHPASS": body.ssh_password}
            proc = await asyncio.create_subprocess_exec(
                "sshpass", "-e", "ssh-copy-id", "-o", "StrictHostKeyChecking=accept-new", box.ssh,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
            assert proc.stdout
            async for line in proc.stdout:
                job.log(line.decode(errors="replace").rstrip())
            if await proc.wait() != 0:
                raise RuntimeError("ssh-copy-id thất bại (sai mật khẩu SSH?)")
            err = await remote.check_ssh(box)
            if err:
                raise RuntimeError(f"đã cài key nhưng vẫn chưa SSH được: {err}")
            job.log("SSH bằng key OK.")
        return jobs.start("copy-id", name, f"Cài SSH key lên {box.ssh}", run).as_dict()

    @app.get("/api/boxes/{name}/info", dependencies=[Depends(auth)])
    async def box_info(name: str) -> Dict[str, Any]:
        return await agent(box_or_404(name), "info", timeout=30)

    @app.get("/api/boxes/{name}/devices", dependencies=[Depends(auth)])
    async def box_devices(name: str) -> Dict[str, Any]:
        return await agent(box_or_404(name), "list", timeout=60)

    @app.get("/api/boxes/{name}/images", dependencies=[Depends(auth)])
    async def box_images(name: str) -> Dict[str, Any]:
        return await agent(box_or_404(name), "images", timeout=30)

    @app.get("/api/boxes/{name}/profiles", dependencies=[Depends(auth)])
    async def box_profiles(name: str) -> Dict[str, Any]:
        return await agent(box_or_404(name), "profiles", timeout=30)

    @app.post("/api/boxes/{name}/provision/{mode}", dependencies=[Depends(auth)])
    async def provision(name: str, mode: str) -> Dict[str, Any]:
        box = box_or_404(name)
        if mode not in PROVISION_MODES:
            raise HTTPException(400, f"mode phải là {', '.join(PROVISION_MODES)}")
        titles = {"check": "Kiểm tra box",
                  "apply": "Chuẩn hoá box (lược hệ vendor + nạp image chính)",
                  "takeover": "Chuẩn hoá box (lược hệ vendor + nạp image chính)",
                  "revert": "Hoàn tác về cấu hình vendor"}

        async def run(job: Job) -> Any:
            rc = await remote.host_script(box, PROVISION_MODES[mode], job.log)
            if rc != 0:
                if mode == "check":
                    job.status = "failed"
                    return {"rc": rc}
                raise RuntimeError(f"df-host.sh exit {rc}")
            if mode == "apply":
                # 1 nút trọn gói cho box mới: lược vendor (đã chạy phía trên) →
                # resetprop (agent cần để áp hồ sơ lúc boot) → image chính.
                await remote.install_resetprop(box, job.log)
                main = box.main_image or store.default_main_image
                if main:
                    await remote.ensure_image(box, main, job.log)
                    job.log(f"✓ box sẵn sàng tạo máy — image chính: {main}")
                else:
                    job.log("⚠ chưa đặt main_image trong boxes.yaml — bỏ qua bước nạp image chính")
            return {"rc": rc}
        return jobs.start("provision", name, titles[mode], run).as_dict()

    # ------------------------------------------------------------------ devices
    async def provision_one(job: Job, box: Box, dev: str, body: DeviceIn,
                            ceiling: float, stop_on_timeout: bool = False) -> Dict[str, Any]:
        """Create one container and wait for Android to boot (raises on failure)."""
        args = ["create", dev, "--width", str(body.width), "--height", str(body.height),
                "--dpi", str(body.dpi), "--fps", str(body.fps)]
        if body.image:
            args += ["--image", body.image]
        if body.profile:
            args += ["--profile", body.profile]
        key = adbkey()
        if key:
            args += ["--adbkey", key]
        job.log(f"Tạo container {dev} ...")
        res = await remote.agent(box, *args, timeout=180)
        job.log(f"Đã tạo {dev}: IP {res['ip']}, binder {res['binders']}, image {res['image'].split('/')[-1]}")
        # container mới: TCP adb cũ (cùng IP) trỏ máy đã xóa — buộc hủy để không treo
        try:
            await adb.reset(box, res["ip"])
        except Exception as e:  # không chặn boot — nếu hỏng thì _with_retry tự cứu
            job.log(f"  ⚠ không reset được adb ({e})")
        job.log(f"Chờ Android boot (tối đa {BOOT_TIMEOUT_S}s; dừng nếu load ≥ {ceiling:g} liên tục {LOAD_GUARD_STRIKES} lần) ...")
        t = 0
        strikes = 0
        while t < BOOT_TIMEOUT_S:
            await asyncio.sleep(POLL_S)
            t += POLL_S
            st = await remote.agent(box, "boot", dev, timeout=30)
            load = _load1(st)
            if not st.get("running"):
                raise RuntimeError("container đã thoát trong lúc boot (xem log)")
            if st.get("boot_completed"):
                job.log(f"Boot xong sau {t}s (load {load:g}).")
                break
            # chỉ dừng khi box quá tải thật sự: load cao LIÊN TỤC, không phải spike nhất thời
            if load >= ceiling:
                strikes += 1
                job.log(f"  ⚠ {t}s load cao {load:g} ≥ {ceiling:g} ({strikes}/{LOAD_GUARD_STRIKES})")
                if strikes >= LOAD_GUARD_STRIKES:
                    await remote.agent(box, "stop", dev, timeout=90)
                    raise RuntimeError(f"load {load:g} ≥ {ceiling:g} liên tục {strikes} lần khi boot → đã dừng máy (box quá tải)")
            else:
                strikes = 0
                if t % 15 == 0:
                    job.log(f"  {t}s, load {load:g}")
        else:
            if stop_on_timeout:
                await remote.agent(box, "stop", dev, timeout=90)
                raise RuntimeError(f"quá {BOOT_TIMEOUT_S}s chưa boot xong → đã dừng máy")
            raise RuntimeError(f"quá {BOOT_TIMEOUT_S}s chưa boot xong (máy vẫn chạy, kiểm tra log)")
        if body.autostart:
            await remote.agent(box, "autostart", dev, "on")
            job.log(f"{dev}: bật tự chạy khi box khởi động lại.")
        return res

    async def wait_settle(job: Job, box: Box, settle: float) -> None:
        """Chờ load 1-phút hạ dưới `settle` trước khi tạo máy kế (tránh dồn tải khi batch)."""
        waited = 0
        while waited < SETTLE_MAX_S:
            info = await remote.agent(box, "info", timeout=30)
            load = _load1(info)
            if load < settle:
                return
            if waited == 0:
                job.log(f"  chờ tải hạ (load {load:g} ≥ {settle:g}) rồi mới tạo máy kế…")
            await asyncio.sleep(SETTLE_POLL_S)
            waited += SETTLE_POLL_S
        job.log(f"  (vẫn tải cao sau {SETTLE_MAX_S}s, vẫn tạo tiếp)")

    async def free_names(box: Box, base: str, count: int) -> list[str]:
        """Pick <count> names '<base>-<i>' skipping ones that already exist."""
        try:
            lst = await agent(box, "list", timeout=60)
            taken = {d["name"] for d in lst.get("devices", [])}
        except Exception:
            taken = set()
        names: list[str] = []
        i = 1
        while len(names) < count:
            cand = f"{base}-{i}"
            if cand not in taken:
                names.append(cand)
            i += 1
            if i > count + len(taken) + 100:   # safety bound
                break
        return names

    @app.post("/api/boxes/{name}/devices", dependencies=[Depends(auth)])
    async def create_device(name: str, body: DeviceIn) -> Dict[str, Any]:
        box = box_or_404(name)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", body.name):
            raise HTTPException(400, "tên máy chỉ gồm a-z 0-9 _ - (tối đa 31 ký tự)")

        async def box_cpus() -> Optional[int]:
            try:
                info = await remote.agent(box, "info", timeout=30)
                return info.get("cpus")
            except Exception:
                return None

        if body.count <= 1:
            async def run(job: Job) -> Any:
                ceiling = _load_ceiling(await box_cpus())
                return await provision_one(job, box, body.name, body, ceiling)
            return jobs.start("create", name, f"Tạo máy {body.name}", run).as_dict()

        # ---- batch: tạo tuần tự, chờ tải hạ giữa các máy, lỗi 1 máy không chặn các máy còn lại
        if len(body.name) > 28:
            raise HTTPException(400, "tên cơ sở quá dài khi tạo hàng loạt (chủa chỗ cho -<i>)")

        async def run_batch(job: Job) -> Any:
            cpus = await box_cpus()
            ceiling = _load_ceiling(cpus)
            settle = float(os.environ.get("FARM_CONSOLE_SETTLE_LOAD", str(cpus or 4)))
            names = await free_names(box, body.name, body.count)
            job.log(f"Tạo {len(names)} máy: {', '.join(names)}  (core={cpus or '?'}, trần load={ceiling:g})")
            ok: list[str] = []
            failed: list[Dict[str, str]] = []
            for idx, dev in enumerate(names, 1):
                job.log(f"—— [{idx}/{len(names)}] {dev} ——")
                if idx > 1:
                    await wait_settle(job, box, settle)
                try:
                    await provision_one(job, box, dev, body, ceiling, stop_on_timeout=True)
                    ok.append(dev)
                except Exception as exc:
                    failed.append({"device": dev, "error": str(exc)})
                    job.log(f"⚠ {dev} LỖI: {exc}")
                    # Máy không boot được vẫn giữ "slot" (loop/dm + phiên TEE) của box — phải
                    # XOÁ hẳn (stop không trả slot) để các máy sau còn chỗ. Máy chưa boot
                    # nên không có dữ liệu gì để mất.
                    try:
                        await remote.agent(box, "rm", dev, "--purge", timeout=90)
                        job.log(f"  đã xoá {dev} (giải phóng slot cho máy sau)")
                    except Exception as e2:
                        job.log(f"  (chưa xoá được {dev}: {e2})")
            job.log(f"Xong: {len(ok)} thành công, {len(failed)} lỗi.")
            if failed and not ok:
                raise RuntimeError("; ".join(f'{f["device"]}: {f["error"]}' for f in failed))
            return {"created": ok, "failed": failed}
        return jobs.start("create", name,
                          f"Tạo {body.count} máy {body.name}-*", run_batch).as_dict()

    @app.post("/api/boxes/{name}/devices/{dev}/input", dependencies=[Depends(auth)])
    async def device_input(name: str, dev: str, body: InputIn) -> Dict[str, Any]:
        box = box_or_404(name)
        t = body.type
        if t == "tap" and body.x is not None and body.y is not None:
            cmd = ["input", "tap", str(body.x), str(body.y)]
        elif t == "swipe" and None not in (body.x, body.y, body.x2, body.y2):
            cmd = ["input", "swipe", str(body.x), str(body.y), str(body.x2), str(body.y2),
                   str(min(max(body.ms or 250, 50), 3000))]
        elif t == "key" and body.key and re.fullmatch(r"[A-Z0-9_]{1,40}", body.key):
            cmd = ["input", "keyevent", body.key]
        elif t == "text" and body.text and re.fullmatch(r"[\w .,@:/+\-?=&#]{1,200}", body.text):
            # adb shell ghép tham số thành 1 dòng lệnh cho sh trên máy: chỉ nhận ký tự an toàn
            cmd = ["input", "text", body.text.replace(" ", "%s")]
        else:
            raise HTTPException(400, "input không hợp lệ")
        if any(not re.fullmatch(r"[\w.,@:/+\-?=&#%]+", c) for c in cmd):
            raise HTTPException(400, "input không hợp lệ")
        await via_adb(box, dev, adb.shell, *cmd)
        return {"name": dev, "input": t}

    @app.post("/api/boxes/{name}/devices/{dev}/{action}", dependencies=[Depends(auth)])
    async def device_action(name: str, dev: str, action: str) -> Dict[str, Any]:
        box = box_or_404(name)
        if action in ("start", "stop", "restart"):
            ips.pop((box.name, dev), None)
            return await agent(box, action, dev, timeout=120)
        if action in ("autostart-on", "autostart-off"):
            return await agent(box, "autostart", dev, action.split("-")[1])
        if action == "scrcpy":
            return await open_scrcpy(box, dev)
        raise HTTPException(400, f"thao tác không hỗ trợ: {action}")

    @app.delete("/api/boxes/{name}/devices/{dev}", dependencies=[Depends(auth)])
    async def delete_device(name: str, dev: str, purge: bool = Query(False)) -> Dict[str, Any]:
        args = ["rm", dev] + (["--purge"] if purge else [])
        return await agent(box_or_404(name), *args, timeout=120)

    @app.get("/api/boxes/{name}/devices/{dev}/screen", dependencies=[Depends(auth)])
    async def screen(name: str, dev: str) -> Response:
        png = await via_adb(box_or_404(name), dev, adb.screencap)
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/boxes/{name}/devices/{dev}/stream.h264", dependencies=[Depends(auth)])
    async def stream_h264(name: str, dev: str, request: Request,
                          size: str = Query("", pattern=r"^(\d{2,4}x\d{2,4})?$"),
                          bitrate: int = Query(4_000_000, ge=500_000, le=20_000_000)) -> StreamingResponse:
        """Stream H.264 Annex-B liên tục (phần cứng encode) — client decode bằng WebCodecs.

        1 kết nối duy nhất thay vì poll từng PNG: hết overhead exec-out mỗi frame và
        ~25x ít băng thông nên không còn `adb timeout`. screenrecord tự dừng sau 180s
        nên ta khởi động lại trong vòng lặp (mỗi lần phát SPS/PPS/IDR mới).
        """
        box = box_or_404(name)
        ip = await device_ip(box, dev)

        async def gen() -> Any:
            proc = None
            try:
                while not await request.is_disconnected():
                    try:
                        proc = await adb.screenrecord(box, ip, size=size, bitrate=bitrate)
                    except RemoteError as e:
                        log.info("stream %s/%s: %s", name, dev, e)
                        ips.pop((box.name, dev), None)
                        return
                    assert proc.stdout
                    while True:
                        chunk = await proc.stdout.read(65536)
                        if not chunk:
                            break
                        yield chunk
                    await proc.wait()
                    proc = None
            finally:
                if proc and proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except ProcessLookupError:
                        pass

        return StreamingResponse(gen(), media_type="video/h264",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/boxes/{name}/devices/{dev}/logs", dependencies=[Depends(auth)])
    async def device_logs(name: str, dev: str, n: int = Query(200, ge=10, le=5000)) -> Dict[str, Any]:
        return await agent(box_or_404(name), "logs", dev, str(n), timeout=30)

    # ------------------------------------------------------------- integrity-fix
    @app.get("/api/keyboxes", dependencies=[Depends(auth)])
    def list_keyboxes() -> Dict[str, Any]:
        items = [kb.public() for kb in keyboxes.list()]   # runs revocation fetch
        return {"dir": str(keyboxes.directory) if keyboxes.directory else None,
                "revocation": keyboxes.revocation_source, "keyboxes": items}

    @app.get("/api/boxes/{name}/devices/{dev}/integrity", dependencies=[Depends(auth)])
    async def integrity_status(name: str, dev: str) -> Dict[str, Any]:
        box = box_or_404(name)
        try:
            return await via_adb(box, dev, integrity.status)
        except IntegrityError as e:
            raise HTTPException(502, str(e))

    @app.post("/api/boxes/{name}/devices/{dev}/integrity/apply", dependencies=[Depends(auth)])
    async def integrity_apply(name: str, dev: str, body: IntegrityIn) -> Dict[str, Any]:
        """Xoay keybox NHÚNG (cơ chế thật, đã chứng minh 2026-11-06):
        patch lib trong layer image → TẠO LẠI máy (giữ image/hồ sơ/kích thước) → status.
        keymaster.xml/magisk/PIF không được dùng nữa (đường xml CHẾT, module không tải)."""
        box = box_or_404(name)

        async def run(job: Job) -> Any:
            # 1. xoá lib trong image layer (validate keybox, keybox_embed, agent ghi)
            info = await integrity.rotate(box, dev, body.keybox, job.log)
            # 2. tạo lại máy nguyên vẹn — docker restart làm mất netns wiring nên PHẢI recreate
            job.log(f"xoá máy {dev} (giữ /data) để boot lại với chain mới …")
            await remote.agent(box, "rm", dev, timeout=90)
            body2 = DeviceIn(name=dev, image=info["image"], profile=info["profile"],
                             width=info["width"], height=info["height"],
                             dpi=info["dpi"], fps=info["fps"])
            try:
                bi = await remote.agent(box, "info", timeout=30)
                cpus = bi.get("cpus")
            except Exception:
                cpus = None
            await provision_one(job, box, dev, body2, _load_ceiling(cpus))
            # 3. đo trạng thái mới — adbd có thể chưa nhận kết nối ngay sau
            #    boot (boot_completed ≠ adbd listen) nên thử vài lần
            st = None
            for i in range(4):
                await asyncio.sleep(5)
                try:
                    st = await via_adb(box, dev, integrity.status)
                    break
                except Exception as e:
                    job.log(f"  ⚠ đo status lần {i + 1} lỗi: {e}")
            if st is None:
                raise RuntimeError("không đo được status sau xoay — adb chưa lên? xem log máy")
            job.log(f"chain mới: {st['chain'].get('name') or st['chain']['md5'][:8]} "
                    f"(device={st['device']}, fixed={st['fixed']})")
            return st
        return jobs.start("integrity-rotate", name,
                          f"Xoay keybox cho {dev}", run).as_dict()

    @app.post("/api/boxes/{name}/devices/{dev}/integrity/remove", dependencies=[Depends(auth)])
    async def integrity_remove(name: str, dev: str) -> Dict[str, Any]:
        box = box_or_404(name)

        async def run(job: Job) -> Any:
            ip = await device_ip(box, dev)
            try:
                st = await integrity.remove(box, ip, job.log)
            except IntegrityError as e:
                raise RuntimeError(str(e))
            job.log("integrity-fix: đã dọn artefact legacy (chain nhúng giữ nguyên).")
            return st
        return jobs.start("integrity-remove", name, f"Dọn legacy integrity cho {dev}", run).as_dict()

    # ------------------------------------------------------------- proxy (egress)
    @app.get("/api/boxes/{name}/devices/{dev}/proxy", dependencies=[Depends(auth)])
    async def proxy_status(name: str, dev: str) -> Dict[str, Any]:
        box = box_or_404(name)
        try:
            return await via_adb(box, dev, proxy.status)
        except ProxyError as e:
            raise HTTPException(502, str(e))

    @app.post("/api/boxes/{name}/devices/{dev}/proxy/set", dependencies=[Depends(auth)])
    async def proxy_set(name: str, dev: str, body: ProxyIn) -> Dict[str, Any]:
        box = box_or_404(name)

        async def run(job: Job) -> Any:
            ip = await device_ip(box, dev)
            try:
                st = await proxy.set(box, ip, host=body.host, port=body.port,
                                     kind=body.kind, user=body.user,
                                     password=body.password, log_fn=job.log)
            except ProxyError as e:
                raise RuntimeError(str(e))
            job.log("proxy: ÁP DỤNG xong.")
            return st
        return jobs.start("proxy-set", name, f"Đặt proxy cho {dev}", run).as_dict()

    @app.post("/api/boxes/{name}/devices/{dev}/proxy/clear", dependencies=[Depends(auth)])
    async def proxy_clear(name: str, dev: str) -> Dict[str, Any]:
        box = box_or_404(name)

        async def run(job: Job) -> Any:
            ip = await device_ip(box, dev)
            try:
                st = await proxy.clear(box, ip, job.log)
            except ProxyError as e:
                raise RuntimeError(str(e))
            job.log("proxy: đã XOÁ, egress trực tiếp.")
            return st
        return jobs.start("proxy-clear", name, f"Xoá proxy cho {dev}", run).as_dict()

    async def open_scrcpy(box: Box, dev: str) -> Dict[str, Any]:
        """Mở scrcpy trên máy đang chạy console (chỉ có ý nghĩa khi console chạy trên laptop)."""
        if not (android_sh and android_sh.exists()):
            raise HTTPException(400, "không tìm thấy tools/android.sh")
        env = {**os.environ, "FARM_BOX": box.ssh, "BOX_SUDO_PASS": box.sudo_password or ""}
        await asyncio.create_subprocess_exec(
            str(android_sh), dev, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        return {"opened": dev}

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs", dependencies=[Depends(auth)])
    def list_jobs(box: Optional[str] = None) -> List[Dict[str, Any]]:
        return jobs.recent(box)

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(auth)])
    def get_job(job_id: int, since: int = Query(0, ge=0)) -> Dict[str, Any]:
        try:
            return jobs.get(job_id).as_dict(since)
        except KeyError:
            raise HTTPException(404, "không có job này")

    app.state.jobs = jobs
    # trang web: mount SAU cùng để /api/* luôn được ưu tiên
    if (WEB_DIR / "index.html").is_file():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    else:
        @app.get("/", include_in_schema=False)
        def no_web() -> PlainTextResponse:
            return PlainTextResponse(f"Chưa build giao diện web ({WEB_DIR}).\n"
                                     "Chạy: cd farm-console/web && npm ci && npm run build", status_code=503)
    return app
