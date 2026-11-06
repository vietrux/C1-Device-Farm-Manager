"""Danh sách box (YAML, quyền 0600 vì có thể chứa mật khẩu sudo)."""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")
# user@host, user@host:port không hỗ trợ (dùng ~/.ssh/config cho port riêng)
SSH_RE = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+$|^[A-Za-z0-9_.-]+$")


class StoreError(ValueError):
    pass


@dataclass
class Box:
    name: str
    ssh: str
    sudo_password: Optional[str] = None
    note: str = ""

    def public(self) -> Dict[str, Any]:
        """Dạng trả cho trình duyệt: không bao giờ kèm mật khẩu."""
        return {"name": self.name, "ssh": self.ssh, "note": self.note,
                "has_password": bool(self.sudo_password)}


@dataclass
class BoxStore:
    path: Path
    boxes: List[Box] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls, path: Path) -> "BoxStore":
        st = cls(path=path)
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}
            for b in raw.get("boxes") or []:
                st.boxes.append(Box(name=str(b["name"]), ssh=str(b["ssh"]),
                                    sudo_password=b.get("sudo_password"), note=str(b.get("note") or "")))
        return st

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"boxes": [{k: v for k, v in asdict(b).items() if v not in (None, "")} for b in self.boxes]}
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def get(self, name: str) -> Box:
        for b in self.boxes:
            if b.name == name:
                return b
        raise KeyError(name)

    def add(self, name: str, ssh: str, sudo_password: Optional[str] = None, note: str = "") -> Box:
        if not NAME_RE.match(name):
            raise StoreError("tên box chỉ gồm chữ, số, _ . - (tối đa 41 ký tự)")
        if not SSH_RE.match(ssh):
            raise StoreError("địa chỉ SSH dạng user@host")
        with self._lock:
            if any(b.name == name for b in self.boxes):
                raise StoreError(f"đã có box '{name}'")
            box = Box(name=name, ssh=ssh, sudo_password=sudo_password or None, note=note)
            self.boxes.append(box)
            self.save()
        log.info("thêm box %s (%s)", name, ssh)
        return box

    def remove(self, name: str) -> None:
        with self._lock:
            before = len(self.boxes)
            self.boxes = [b for b in self.boxes if b.name != name]
            if len(self.boxes) == before:
                raise KeyError(name)
            self.save()
        log.info("xoá box %s khỏi console", name)
