"""Keybox pool — load + validate the keyboxes the farm can install.

A keybox is an ``AndroidAttestation`` XML holding one or more attestation keys
(ecdsa/rsa) with their certificate chain. ``integrity apply`` installs one into
a container (via the vendor ``/uploadkeybox``) so hardware attestation produces
a Google-rooted, non-revoked chain — the DEVICE half of Play Integrity.

This module does NOT ship keyboxes (they are sensitive and rotate); it reads a
directory of ``*.xml`` the operator points at (``KEYBOX_DIR``) and reports each
one's health so the console can list sources and pick the best.

Validation mirrors tools/integrity/roots + kbcheck: chain linkage + signatures,
self-signed root matched against known root pubkeys, validity window, and
revocation against Google's status list (online, cached fallback).
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, ed448, padding, rsa

ROOTS_DIR = Path(__file__).resolve().parent.parent.parent / "tools" / "integrity" / "roots"
GOOGLE_STATUS_URL = "https://android.googleapis.com/attestation/status"
#: root families the community treats as valid for Play Integrity DEVICE.
GOOD_ROOTS = ("google", "droid_rkp")
REVOCATION_TTL = 3600.0


# --------------------------------------------------------------------------- crypto helpers
def _load_roots() -> Dict[str, Any]:
    roots: Dict[str, Any] = {}
    for name in ("google", "aosp_ec", "aosp_rsa", "knox", "droid_rkp"):
        p = ROOTS_DIR / f"{name}.pem"
        if p.exists():
            roots[name] = serialization.load_pem_public_key(p.read_bytes())
    return roots


def _pub_pem(pub: Any) -> bytes:
    return pub.public_bytes(serialization.Encoding.PEM,
                            serialization.PublicFormat.SubjectPublicKeyInfo)


def _pub_matches(a: Any, b: Any) -> bool:
    try:
        return _pub_pem(a) == _pub_pem(b)
    except Exception:
        return False


def _verify_sig(child: x509.Certificate, issuer: x509.Certificate) -> None:
    pub = issuer.public_key()
    if isinstance(pub, ec.EllipticCurvePublicKey):
        pub.verify(child.signature, child.tbs_certificate_bytes, ec.ECDSA(child.signature_hash_algorithm))
    elif isinstance(pub, rsa.RSAPublicKey):
        try:
            pub.verify(child.signature, child.tbs_certificate_bytes,
                       padding.PKCS1v15(), child.signature_hash_algorithm)
        except Exception:
            pub.verify(child.signature, child.tbs_certificate_bytes,
                       padding.PSS(mgf=padding.MGF1(child.signature_hash_algorithm),
                                   salt_length=child.signature_hash_algorithm.digest_size),
                       child.signature_hash_algorithm)
    elif isinstance(pub, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
        pub.verify(child.signature, child.tbs_certificate_bytes)
    else:
        raise ValueError("unsupported key type")


# --------------------------------------------------------------------------- revocation
class _Revocation:
    """Google's revoked-serial list, fetched online with a cached fallback."""

    def __init__(self) -> None:
        self._entries: Dict[str, Any] = {}
        self._source = "none"
        self._at = 0.0

    def entries(self) -> Dict[str, Any]:
        if time.monotonic() - self._at < REVOCATION_TTL and self._source != "none":
            return self._entries
        try:
            import requests  # optional dependency; fall back to cache if absent
            r = requests.get(GOOGLE_STATUS_URL, headers={"Cache-Control": "no-cache"},
                             params={"ts": int(time.time())}, timeout=12)
            r.raise_for_status()
            self._entries = r.json().get("entries", {})
            self._source = "online"
        except Exception as e:
            p = ROOTS_DIR / "status.json"
            try:
                self._entries = json.loads(p.read_text()).get("entries", {})
                self._source = f"cache ({type(e).__name__})"
            except Exception:
                self._entries, self._source = {}, "unavailable"
        self._at = time.monotonic()
        return self._entries

    @property
    def source(self) -> str:
        return self._source


# --------------------------------------------------------------------------- model
@dataclass
class KeyInfo:
    algorithm: str
    root: Optional[str] = None
    not_after: Optional[str] = None
    ok: bool = False
    errors: List[str] = field(default_factory=list)


@dataclass
class KeyboxInfo:
    name: str                       # filename stem, shown in the UI
    path: str
    device_id: str = "?"
    keys: List[KeyInfo] = field(default_factory=list)
    usable: bool = False            # ≥1 Google/droid-rooted key, chain ok, in-date, not revoked
    revoked: bool = False
    not_after: Optional[str] = None  # soonest expiry among keys (kể cả revoked)
    algorithms: List[str] = field(default_factory=list)
    score: float = 0.0
    error: Optional[str] = None     # file-level parse error

    def public(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


def _parse(path: Path) -> List[tuple]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"<\?xml|<AndroidAttestation>", raw)
    if m and m.start() > 0:
        raw = raw[m.start():]
    root = ET.fromstring(raw)
    out = []
    for kb in root.iter("Keybox"):
        dev = kb.get("DeviceID", "?")
        for key in kb.findall("Key"):
            algo = key.get("algorithm", "?")
            priv_el, chain_el = key.find("PrivateKey"), key.find("CertificateChain")
            if priv_el is None or chain_el is None:
                continue
            certs = [(c.text or "").strip() for c in chain_el.findall("Certificate") if c.text]
            out.append((dev, algo, (priv_el.text or "").strip(), certs))
    return out


def _check_key(algo: str, priv_pem: str, certs: List[str], roots: Dict[str, Any],
               entries: Dict[str, Any]) -> KeyInfo:
    r = KeyInfo(algorithm=algo)
    try:
        priv = serialization.load_pem_private_key(priv_pem.encode(), password=None)
        chain = [x509.load_pem_x509_certificate(c.encode()) for c in certs]
    except Exception as e:
        r.errors.append(f"parse: {e}")
        return r
    if not chain:
        r.errors.append("empty_chain")
        return r
    leaf = chain[0]
    now = time.time()
    r.not_after = leaf.not_valid_after_utc.isoformat()
    if now < leaf.not_valid_before_utc.timestamp() or now > leaf.not_valid_after_utc.timestamp():
        r.errors.append("expired")
    if not _pub_matches(priv.public_key(), leaf.public_key()):
        r.errors.append("private_key_mismatch")
    for i in range(len(chain) - 1):
        c, iss = chain[i], chain[i + 1]
        if c.issuer != iss.subject:
            r.errors.append(f"chain_name@{i}")
            continue
        try:
            _verify_sig(c, iss)
        except Exception as e:
            r.errors.append(f"chain_sig@{i}: {e}")
    rcert = chain[-1]
    if rcert.subject != rcert.issuer:
        r.errors.append("root_not_selfsigned")
    else:
        for name, key in roots.items():
            if _pub_matches(rcert.public_key(), key):
                r.root = name
                break
        if r.root is None:
            r.errors.append("root_unknown")
    for c in chain:
        sn = hex(c.serial_number)[2:].lower()
        ent = entries.get(sn)
        if ent:
            r.errors.append(f"REVOKED: {ent.get('reason', '?')}")
    r.ok = not r.errors and r.root in GOOD_ROOTS
    return r


def _score(kb: KeyboxInfo) -> float:
    if not kb.usable:
        return 0.0
    s = 10.0
    if any(k.root == "google" for k in kb.keys if k.ok):
        s += 10.0                                  # classic hardware root > RKP
    s += min(len([k for k in kb.keys if k.ok]), 2) * 3.0   # 2 algos better than 1
    if kb.not_after:                               # longer validity = higher
        try:
            days = (time.mktime(time.strptime(kb.not_after[:10], "%Y-%m-%d")) - time.time()) / 86400
            s += max(min(days / 30.0, 20.0), 0.0)
        except Exception:
            pass
    return round(s, 2)


class KeyboxPool:
    """All keyboxes under ``directory``, validated on demand."""

    def __init__(self, directory: Optional[Path]) -> None:
        self.directory = Path(directory) if directory else None
        self._roots = _load_roots()
        self._rev = _Revocation()

    def _validate(self, path: Path) -> KeyboxInfo:
        info = KeyboxInfo(name=path.stem, path=str(path))
        try:
            keys = _parse(path)
        except Exception as e:
            info.error = f"parse: {e}"
            return info
        entries = self._rev.entries()
        info.keys = [_check_key(a, p, c, self._roots, entries) for _, a, p, c in keys]
        if keys:
            info.device_id = keys[0][0]
        info.algorithms = sorted({k.algorithm for k in info.keys})
        usable = [k for k in info.keys if k.ok]
        info.usable = bool(usable)
        info.revoked = any("REVOKED" in e for k in info.keys for e in k.errors)
        # hạn sớm nhất trong MỌI key — keybox revoked vẫn cần biết nó chết thật khi nào
        # (revoked ≠ hết hạn: revoked là gắn cờ, hết hạn mới là chain không verify được)
        exps = [k.not_after for k in info.keys if k.not_after]
        info.not_after = min(exps) if exps else None
        info.score = _score(info)
        return info

    def list(self) -> List[KeyboxInfo]:
        if not self.directory or not self.directory.is_dir():
            return []
        out = [self._validate(p) for p in sorted(self.directory.glob("*.xml"))]
        out.sort(key=lambda k: (-k.score, k.name))
        return out

    def get(self, name: str) -> Optional[KeyboxInfo]:
        for kb in self.list():
            if kb.name == name:
                return kb
        return None

    def best(self) -> Optional[KeyboxInfo]:
        usable = [k for k in self.list() if k.usable]
        return usable[0] if usable else None

    @property
    def revocation_source(self) -> str:
        return self._rev.source
