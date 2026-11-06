"""Integrity-fix — Play Integrity DEVICE+BASIC cho container, theo cơ chế ĐÃ CHỨNG MINH.

2026-11-06 (sau khi debug tận gốc trên máy thật):

  * DEVICE — chuỗi keybox **nhúng** trong ``/vendor/lib64/libsoft_attestation_cert.so``
    của image (vùng @0x82254000 trong super.img). HAL keymaster **không bao giờ đọc**
    ``/data/system/keymaster.xml`` (atime không đổi qua nhiều attest tươi, kể cả
    permissive) — đường xml là CHẾT. Xoay vòng = thay data lib trong layer của image
    rồi **tạo lại máy** (docker restart làm mất netns wiring), xem GOLDEN-BAKE.md.
  * BASIC — props thiết bị do MYTPIF (dựng sẵn trong framework) + hồ sơ resetprop;
    không cần Magisk/Zygisk/PIF (module không tải nổi trong container).
  * Magisk/PIF/keymaster.xml vẫn được báo cáo (legacy, để đối chiếu) nhưng
    **không** tính vào trạng thái "fixed".

Luồng status: ``status.sh`` chạy trong máy đo SỰ THẬT THÔ (md5 lib chain, props
thật, app); module này đối chiếu md5 với sổ đăng ký chain
(``~/.cache/devicefarm/integrity/chains.json``) để gọi tên keybox + revoked/hết hạn.

Luồng apply (xoay vòng): laptop tính lib đã patch bằng ``tools/box/keybox_embed.py``
từ lib gốc của image (agent ``image-lib``), đẩy lên box, agent ``patch-image-lib``
ghi vào layer, ghi sổ, rồi app.py **tạo lại máy** giữ nguyên image/hồ sơ/kích thước.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from .adb import Adb
from .keybox import KeyboxPool
from .remote import Remote, RemoteError
from .store import Box

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEVICE_DIR = REPO_ROOT / "tools" / "integrity" / "device"
KEYBOX_EMBED = REPO_ROOT / "tools" / "box" / "keybox_embed.py"
REMOTE_TMP = "/data/local/tmp"
LogFn = Callable[[str], None]

#: md5 lib gốc của các image đã biết — ghi sổ lần đầu xoay/seed tay (NOTES.md).
FACTORY_LIB_MD5 = "4bafc9913b11ff33f94354109ff2a35f"      # vendor gốc (chain công khai, bị revoke)


class IntegrityError(Exception):
    pass


# --------------------------------------------------------------------- sổ chain
class ChainRegistry:
    """md5 lib → thông tin keybox nhúng. Ghi khi xoay vòng / bake; đọc khi status."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: Dict[str, Dict[str, Any]] = {}
        self._mtime: float = -1.0
        self._load()

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:            # seed/sửa file ngoài → tự thấy ngay
                self._data = json.loads(self.path.read_text())
                self._mtime = mtime
        except Exception:
            self._data = {}

    def get(self, md5: str) -> Optional[Dict[str, Any]]:
        self._load()
        return self._data.get(md5)

    def put(self, md5: str, entry: Dict[str, Any]) -> None:
        self._data[md5] = entry
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=1, ensure_ascii=False))
        except Exception as e:      # sổ ghi hỏng không chặn xoay vòng
            log.warning("chain registry: không ghi được %s: %s", self.path, e)


def _default_registry_path() -> Path:
    import os
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "devicefarm" / "integrity" / "chains.json"


def _default_assets() -> Path:
    import os
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "devicefarm" / "integrity"


def _key(kb: Any, algo: str) -> Optional[Any]:
    return next((k for k in getattr(kb, "keys", [])
                 if getattr(k, "algorithm", "") == algo), None)


def _chain_revoked(kb: Any) -> bool:
    """Chain serve = nửa EC (Google attestation dùng ECDSA). Pool đánh
    keybox revoked nếu BẤT KỲ nửa nào revoke — nhưng chỉ nửa được serve mới
    quyết định verdict; keybox có RSA revoked nhưng EC sạch phải hiện là sạch."""
    ec = _key(kb, "ecdsa")
    if ec is not None:
        return not getattr(ec, "ok", True)
    return bool(getattr(kb, "revoked", False))


def _chain_note(kb: Any) -> str:
    parts = []
    for algo, label in (("ecdsa", "EC"), ("rsa", "RSA")):
        k = _key(kb, algo)
        if k is None:
            continue
        ok = getattr(k, "ok", False)
        errs = ", ".join(getattr(k, "errors", []) or [])
        parts.append(f"{label} {'sạch' if ok else 'revoked' + (f' ({errs})' if errs else '')}")
    return " · ".join(parts) or "?"


class Integrity:
    def __init__(self, adb: Adb, keyboxes: KeyboxPool, remote: Optional[Remote] = None,
                 assets_dir: Optional[Path] = None, device_dir: Path = DEVICE_DIR,
                 registry_path: Optional[Path] = None) -> None:
        self.adb = adb
        self.keyboxes = keyboxes
        self.remote = remote
        self.assets_dir = Path(assets_dir) if assets_dir else _default_assets()
        self.device_dir = Path(device_dir)
        self.registry = ChainRegistry(registry_path or _default_registry_path())
        self._base_libs: Dict[str, tuple[str, bytes]] = {}   # image → (md5, blob)

    # ------------------------------------------------------------------ status
    async def status(self, box: Box, ip: str) -> Dict[str, Any]:
        """Trạng thái integrity THẬT của máy (đo từ sự thật, không bao giờ "đoán").
        Timeout rộng: status.sh chờ pm sống lại sau xoay (máy mới boot)."""
        await self._push_script(box, ip, "status.sh")
        out = await self.adb.shell(box, ip, "sh", f"{REMOTE_TMP}/status.sh", timeout=120)
        line = next((l for l in out.splitlines() if l.strip().startswith("{")), "")
        try:
            raw = json.loads(line)
        except Exception:
            raise IntegrityError(f"status.sh gave no JSON: {out.strip()[:200]}")
        return self._interpret(raw)

    def _interpret(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Gọi tên chain từ sổ + tính verdict trung thực."""
        md5 = (raw.get("chain_lib_md5") or "").strip()
        entry = self.registry.get(md5) if md5 else None
        chain: Dict[str, Any] = {"md5": md5 or "?", "name": None, "known": bool(entry),
                                 "revoked": None, "expires": None, "note": None}
        device = "unknown"
        if entry:
            chain.update(name=entry.get("name"), revoked=bool(entry.get("revoked")),
                         expires=entry.get("expires"), note=entry.get("note"))
            device = "revoked" if entry.get("revoked") else "ok"
        elif md5 == FACTORY_LIB_MD5:
            chain.update(name="factory (vendor)", revoked=True,
                         note="chain công khai của vendor — Google đã revoke")
            device = "factory"
        # hết hạn (so hạn của keybox nửa EC — nguồn DEVICE thật)?
        exp = chain.get("expires")
        if device == "ok" and exp:
            try:
                from datetime import datetime, timezone
                if datetime.fromisoformat(exp) < datetime.now(timezone.utc):
                    device = "expired"
                    chain["note"] = "chain đã hết hạn"
            except Exception:
                pass
        fp = raw.get("prop_fingerprint") or ""
        locked = (raw.get("flash_locked") or "").strip()
        basic_ok = bool(fp) and locked == "1"
        apps = {"keyattestation": bool(raw.get("keyattestation")),
                "spic": bool(raw.get("spic"))}
        return {
            "chain": chain,
            "device": device,                       # ok | revoked | expired | factory | unknown
            "basic": {"fingerprint": fp, "flash_locked": locked,
                      "vbmeta_state": raw.get("vbmeta_state") or "", "ok": basic_ok},
            "apps": apps,
            "fixed": device == "ok" and basic_ok,
            # legacy — không ảnh hưởng PI trong container, chỉ để đối chiếu
            "legacy": {"magisk": raw.get("magisk") or "", "daemon": bool(raw.get("daemon")),
                       "zygisk": raw.get("zygisk") or "", "pif_module": bool(raw.get("pif_module")),
                       "pif_fingerprint": raw.get("pif_fingerprint") or "",
                       "keymaster_xml": bool(raw.get("keymaster_xml")),
                       "keymaster_xml_device": raw.get("keymaster_xml_device") or ""},
        }

    # ------------------------------------------------------------------ rotate
    async def rotate(self, box: Box, dev: str, keybox: Optional[str],
                     log_fn: Optional[LogFn] = None) -> Dict[str, Any]:
        """Xoay keybox NHÚNG cho `dev`: patch lib trong layer của image rồi trả
        thông tin để app.py tạo lại máy nguyên vẹn (image/hồ sơ/kích thước)."""
        say = log_fn or (lambda _s: None)
        if self.remote is None:
            raise IntegrityError("console chưa cấu hình remote — không xoay được keybox")
        kb = self._resolve_keybox(keybox, say)
        if kb is None:
            raise IntegrityError("không có keybox để xoay (pool rỗng?)")

        say(f"đọc cấu hình máy {dev} …")
        info = await self.remote.agent(box, "dev-info", dev, timeout=60)
        image = info.get("image")
        if not image:
            raise IntegrityError(f"máy {dev} không rõ đang dùng image nào")
        say(f"máy đang dùng image {image.split('/')[-1]}, hồ sơ {info.get('profile') or 'auto'}")

        base_md5, base = await self._base_lib(box, image, say)
        say(f"lib gốc của image: md5 {base_md5[:8]}…")

        patched_path, patched_md5 = self._embed_keybox(base, kb, base_md5, say)
        say(f"lib đã xoá: md5 {patched_md5[:8]}… (keybox {kb.name})")

        remote_lib = f"/tmp/farm-chain-{patched_md5}.so"
        await self.remote.write_file(box, remote_lib, patched_path.read_bytes())
        res = await self.remote.agent(box, "patch-image-lib", image, remote_lib, timeout=120)
        if res.get("md5") != patched_md5:
            raise IntegrityError(f"patch xong md5 khác kỳ vọng ({res.get('md5')} ≠ {patched_md5})")
        say(f"đã ghi vào layer image: {res.get('super')}")

        self.registry.put(patched_md5, {
            "name": kb.name, "revoked": _chain_revoked(kb), "expires": kb.not_after,
            "note": f"{kb.name}: {_chain_note(kb)}",
        })
        return {"image": image, "profile": info.get("profile") or "auto",
                "width": int(info.get("width") or 720), "height": int(info.get("height") or 1280),
                "dpi": int(info.get("dpi") or 320), "fps": int(info.get("fps") or 24),
                "chain_md5": patched_md5, "keybox": kb.name}

    async def _base_lib(self, box: Box, image: str, say: LogFn) -> tuple[str, bytes]:
        cached = self._base_libs.get(image)
        if cached:
            return cached
        say(f"đọc lib chain từ image (lần đầu, cache lại) …")
        res = await self.remote.agent(box, "image-lib", image, timeout=120)
        md5 = res.get("md5") or ""
        blob = base64.b64decode(res.get("b64") or "")
        if not md5 or len(blob) < 1024:
            raise IntegrityError(f"agent image-lib trả về rỗng cho {image}")
        self._base_libs[image] = (md5, blob)
        return md5, blob

    def _embed_keybox(self, base: bytes, kb: Any, base_md5: str,
                      say: LogFn) -> tuple[Path, str]:
        """Chạy tools/box/keybox_embed.py trên lib gốc → lib mới (data-only)."""
        if not KEYBOX_EMBED.exists():
            raise IntegrityError(f"thiếu {KEYBOX_EMBED}")
        with tempfile.TemporaryDirectory(prefix="farm-chain-") as td:
            base_p = Path(td) / "base.so"
            out_p = Path(td) / "patched.so"
            base_p.write_bytes(base)
            r = subprocess.run(
                [sys.executable, str(KEYBOX_EMBED), "--lib", str(base_p),
                 "--keybox", kb.path, "--out", str(out_p)],
                capture_output=True, text=True, timeout=120)
            if r.returncode != 0 or not out_p.exists():
                msg = (r.stderr or r.stdout or "").strip().splitlines()
                raise IntegrityError(msg[-1] if msg else "keybox_embed.py failed")
            blob = out_p.read_bytes()
            md5 = hashlib.md5(blob).hexdigest()
            keep = self.assets_dir / "chain-libs"
            keep.mkdir(parents=True, exist_ok=True)
            final = keep / f"{md5}.so"
            if not final.exists():
                final.write_bytes(blob)
        for ln in (r.stdout or "").splitlines():
            if ln.strip():
                say(ln.rstrip())
        return final, md5

    # ------------------------------------------------------------------ remove
    async def remove(self, box: Box, ip: str, log_fn: Optional[LogFn] = None) -> Dict[str, Any]:
        """Gỡ DI SẢN legacy (magisk/PIF/keymaster.xml) — không đổi chain nhúng.
        Xoay về chain khác = dùng apply với keybox khác."""
        say = log_fn or (lambda _s: None)
        say("dọn artefact legacy (magisk/PIF/keymaster.xml) …")
        await self._push_script(box, ip, "remove.sh")
        out = await self.adb.shell(box, ip, "sh", f"{REMOTE_TMP}/remove.sh", timeout=90)
        for ln in out.splitlines():
            if ln.strip():
                say(ln.rstrip())
        if "REMOVE_OK" not in out:
            raise IntegrityError(out.strip()[:200] or "remove.sh gave no result")
        return await self.status(box, ip)

    # ------------------------------------------------------------------ helpers
    def _resolve_keybox(self, name: Optional[str], say: Optional[LogFn] = None):
        """Chọn keybox theo TÊN — TỰ DO (kể cả revoked/hết hạn/root lạ: operator
        tự quyết — kinh nghiệm cho thấy enforcement thực tế lỏng hơn danh sách
        chính thức, vd trickbox revoked vẫn chạy ngon cả farm) — hoặc để trống
        thì lấy điểm cao nhất; không còn gì usable thì fallback keybox đầu tiên.
        Cửa kỹ thuật thật sự nằm ở keybox_embed (layout phải fit)."""
        warn = say or (lambda _s: None)
        if name == "none":
            return None
        if name:
            kb = self.keyboxes.get(name)
            if kb is None:
                raise IntegrityError(f"no keybox '{name}' in pool")
        else:
            kbs = self.keyboxes.list()
            if not kbs:
                raise IntegrityError("pool keybox trống — không có gì để xoay")
            kb = next((k for k in kbs if k.usable), kbs[0])
        if not getattr(kb, "usable", False):
            warn(f"⚠ keybox '{kb.name}' bị đánh revoked/hết hạn/root lạ theo validator — vẫn xoay theo lựa chọn của bạn")
            warn("   (danh sách revoke = dữ liệu chính thức của Google; enforcement thực tế có thể lỏng — đo lại bằng app để biết chắc)")
        return kb

    async def _push_script(self, box: Box, ip: str, name: str) -> None:
        p = self.device_dir / name
        if not p.exists():
            raise IntegrityError(f"missing device script {p}")
        try:
            await self.adb.push(box, ip, str(p), f"{REMOTE_TMP}/{name}")
        except RemoteError as e:
            raise IntegrityError(str(e))
