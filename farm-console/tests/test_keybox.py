"""KeyboxPool: parse, validate, score, pick best. Uses self-signed fixtures so
the test is offline and deterministic (no real Google-rooted keys needed)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from console import keybox as kbmod
from console.keybox import KeyboxPool

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _self_signed(cn: str, days: int = 3650):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=days))
            .sign(key, hashes.SHA256()))
    return key, cert


def _keybox_xml(dev: str, key, cert) -> str:
    priv = key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()).decode()
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return (f'<?xml version="1.0"?>\n<AndroidAttestation>\n<NumberOfKeyboxes>1</NumberOfKeyboxes>\n'
            f'<Keybox DeviceID="{dev}"><Key algorithm="ecdsa">'
            f'<PrivateKey format="pem">{priv}</PrivateKey>'
            f'<CertificateChain><NumberOfCertificates>1</NumberOfCertificates>'
            f'<Certificate format="pem">{pem}</Certificate></CertificateChain>'
            f'</Key></Keybox>\n</AndroidAttestation>\n')


@pytest.fixture
def pool(tmp_path, monkeypatch):
    # make our self-signed root a "known good" root, and stub revocation offline.
    key, cert = _self_signed("Fixture Root")
    monkeypatch.setattr(kbmod, "_load_roots", lambda: {"google": cert.public_key()})
    monkeypatch.setattr(kbmod, "GOOD_ROOTS", ("google", "droid_rkp"))
    monkeypatch.setattr(kbmod._Revocation, "entries", lambda self: {})
    (tmp_path / "good.xml").write_text(_keybox_xml("Good-Device", key, cert))
    # one with an unknown (different) root -> not usable
    k2, c2 = _self_signed("Other Root")
    (tmp_path / "unknown.xml").write_text(_keybox_xml("Other", k2, c2))
    (tmp_path / "junk.xml").write_text("not even xml")
    return KeyboxPool(tmp_path)


def test_lists_and_validates(pool):
    items = {kb.name: kb for kb in pool.list()}
    assert items["good"].usable is True
    assert items["good"].device_id == "Good-Device"
    assert items["good"].algorithms == ["ecdsa"]
    assert items["unknown"].usable is False
    assert items["junk"].error is not None


def test_best_is_usable_highest_score(pool):
    best = pool.best()
    assert best is not None and best.name == "good"


def test_get_by_name_and_missing(pool):
    assert pool.get("good").usable
    assert pool.get("nope") is None


def test_revoked_marked(tmp_path, monkeypatch):
    key, cert = _self_signed("R")
    monkeypatch.setattr(kbmod, "_load_roots", lambda: {"google": cert.public_key()})
    sn = hex(cert.serial_number)[2:].lower()
    monkeypatch.setattr(kbmod._Revocation, "entries", lambda self: {sn: {"reason": "KEY_COMPROMISE"}})
    (tmp_path / "revoked.xml").write_text(_keybox_xml("R", key, cert))
    kb = KeyboxPool(tmp_path).get("revoked")
    assert kb.revoked is True and kb.usable is False


def test_empty_dir():
    assert KeyboxPool(None).list() == []
