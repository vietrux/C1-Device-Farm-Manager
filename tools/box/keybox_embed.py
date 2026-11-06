#!/usr/bin/env python3
"""Swap the keybox EMBEDDED inside vendor libsoft_attestation_cert.so.

Why: on the Q12 vendor stack, software keymaster loads the VENDOR copy of
libsoft_attestation_cert.so.  When its license check (check_tee) FAILS
(no TEE license after box reboot), keymaster IGNORES /data/system/keymaster.xml
and falls back to a keybox baked INTO the .so — a widely-circulated public
keybox that Google has REVOKED (leaf serial d85fc4b7..., KEY_COMPROMISE).
Key Attestation then reports "Certificate chain is not trusted".

Fix (pure DATA, no code patch — boot-safe): rewrite the embedded blobs
(private keys + cert chains) with OUR keybox and update the {ptr,len} tables:
  - .data.rel.ro @0x110008 : 4 x {u64 ptr, u64 len} (RSA-3072, EC, EC, RSA-1024)
  - .data       @0x121410 : {ptr,len} pairs per cert (file off = vaddr - 0x1000)
  - .rodata     blobs     : raw DER, contiguous region 0x2bbd5..0x2d886

Untouched (do not clobber): signature gap 0x2b9ee..0x2bbd5, RSA-1024 key
@0x2b974 (609B), secondary chain blobs from 0x2d886 (root2 etc.).

Layout contract (must fit before 0x2d886):
  0x2bbd5 EC key | 0x2bc4e EC key | 0x2bcc7 RSA key + pad
  0x2c3a8 EC leaf -> EC int -> EC root -> RSA leaf -> RSA int (shared root)

Usage:
  keybox_embed.py --lib vendor_libsoft.so --keybox trickbox.xml [--out patched.so]
  keybox_embed.py --super super.img --keybox trickbox.xml   # in-place @0x82254000
  keybox_embed.py --lib ... --verify-only

The keybox XML must contain BOTH <Key algorithm="ecdsa"> and <Key
algorithm="rsa">, chains of 3 certs, and the same root cert for both
(Google attestation keyboxes share the root).  Sizes must fit the layout
(see assert_layout).
"""
import argparse
import hashlib
import pickle
import re
import struct
import sys

# ---- vendor lib facts (md5 4bafc9913b11ff33f94354109ff2a35f, 1183168 B) ----
LIB_SIZE = 1183168
ORIG_MD5 = "4bafc9913b11ff33f94354109ff2a35f"

KEY_EC1 = 0x2BBD5                # 2 EC key slots (same size, no table change)
KEY_EC2 = 0x2BC4E
KEY_RSA = 0x2BCC7
KEY_RSA1024 = 0x2B974           # untouched (609 B)
GAP = (0x2B9EE, 0x2BBD5)        # untouched signature region
TAIL = 0x2E2EE                 # 0x2d886..0x2e2ee = secondary chain (4 cert, serial 0x1001-0x1004)
                               # = DATA CHẾT: không bảng {ptr,len} nào đọc (0x121470+ không được
                               # tham chiếu), không adrp/add nào trỏ 0x2d000/0x2e000 trong text
                               # (verify 2026-11-06). Mượn làm chỗ tràn cho chain lớn (keybox lớn).

BLOB_START = 0x2C3A8           # first byte available for new cert layout

T_RSA_KEY_LEN = 0x110010       # .data.rel.ro: RSA key len (u64)
T_CERTS = {                     # .data vaddr -> meaning (file off = vaddr - 0x1000)
    0x121410: "ec-leaf-ptr", 0x121418: "ec-leaf-len",
    0x121420: "ec-int-ptr",  0x121428: "ec-int-len",
    0x121430: "ec-root-ptr", 0x121438: "ec-root-len",
    0x121440: "rsa-leaf-ptr", 0x121448: "rsa-leaf-len",
    0x121450: "rsa-int-ptr",  0x121458: "rsa-int-len",
    0x121460: "rsa-root-ptr", 0x121468: "rsa-root-len",
}


def die(msg):
    sys.exit("[keybox_embed] " + msg)


# ---------------------------------------------------------------- keybox xml
def parse_keybox(path):
    """-> dict with ecdsa_key, ecdsa_certs[3], rsa_key, rsa_certs[3] (DER)."""
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
        load_pem_private_key)

    xml = open(path, encoding="utf-8", errors="ignore").read()
    out = {}
    for algo, body in re.findall(r'<Key algorithm="(\w+)">(.*?)</Key>', xml, re.S):
        if algo not in ("ecdsa", "rsa"):
            continue
        m = re.search(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            body, re.S)
        if not m:
            die(f"{path}: thiếu PrivateKey cho {algo}")
        key = load_pem_private_key(m.group(0).encode(), password=None)
        certs = [x509.load_pem_x509_certificate(c.encode()) for c in
                 re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                            body, re.S)]
        if len(certs) != 3:
            die(f"{path}: chain {algo} phải có đúng 3 cert (thấy {len(certs)})")
        # consistency: key matches leaf, leaf<-int<-root signatures verify
        leaf_pk = certs[0].public_key().public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        key_pk = key.public_key().public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        if leaf_pk != key_pk:
            die(f"{path}: private key {algo} KHÔNG khớp pubkey của leaf cert")
        for c, signer in ((certs[0], certs[1]), (certs[1], certs[2])):
            try:
                c.verify_directly_issued_by(signer)
            except Exception:
                die(f"{path}: chữ ký chain {algo} không verify ({c.subject})")
        out[algo + "_key"] = key.private_bytes(
            Encoding.DER, PrivateFormat.TraditionalOpenSSL, NoEncryption())
        for i, c in enumerate(certs):
            out[f"{algo}_cert{i}"] = c.public_bytes(Encoding.DER)
    for need in ("ecdsa_key", "ecdsa_cert0", "rsa_key", "rsa_cert0"):
        if need not in out:
            die(f"{path}: thiếu {need} (cần cả 2 algorithm ecdsa+rsa)")
    if out["ecdsa_cert2"] != out["rsa_cert2"]:
        die(f"{path}: root cert của 2 chain phải LÀ MỘT (shared root)")
    return out


def assert_layout(kb):
    """Sizes must fit: certs start 0x2c3a8, everything ends before TAIL (0x2e2ee —
    kể cả vùng secondary chain chết 0x2d886+)."""
    need = sum(len(kb[x]) for x in
               ("ecdsa_cert0", "ecdsa_cert1", "ecdsa_cert2",
                "rsa_cert0", "rsa_cert1"))
    avail = TAIL - BLOB_START
    if need > avail:
        die(f"chain quá lớn: cần {need}B, vùng chứa {avail}B "
            f"(trước 0x{TAIL:x}) — xếp lại layout hoặc chọn keybox nhỏ hơn")
    if len(kb["rsa_key"]) > 0x6e9:
        die(f"RSA key quá lớn cho slot: {len(kb['rsa_key'])}B > 1769B")
    if len(kb["ecdsa_key"]) != 121:
        die(f"EC key phải là SEC1 P-256 121B (thấy {len(kb['ecdsa_key'])}B)")


# ------------------------------------------------------------------- patch
def patch_lib(raw, kb):
    """raw: bytearray of the .so — modified in place."""
    def wr(off, data):
        raw[off:off + len(data)] = data

    # keys (EC slots same size; RSA len updated in table)
    wr(KEY_EC1, kb["ecdsa_key"])
    wr(KEY_EC2, kb["ecdsa_key"])
    wr(KEY_RSA, kb["rsa_key"])
    raw[KEY_RSA + len(kb["rsa_key"])] = 0  # pad byte(s) up to BLOB_START

    # cert chain, packed from BLOB_START
    pos = BLOB_START
    ptrs = {}
    for name in ("ecdsa_cert0", "ecdsa_cert1", "ecdsa_cert2",
                 "rsa_cert0", "rsa_cert1"):
        wr(pos, kb[name])
        ptrs[name] = (pos, len(kb[name]))
        pos += len(kb[name])
    if pos > TAIL:
        die(f"layout tràn: kết thúc 0x{pos:x} > 0x{TAIL:x}")

    # tables
    def setq(vaddr, val):
        off = vaddr if vaddr < 0x120000 else vaddr - 0x1000
        struct.pack_into("<Q", raw, off, val)

    setq(T_RSA_KEY_LEN, len(kb["rsa_key"]))
    setq(0x121410, ptrs["ecdsa_cert0"][0]); setq(0x121418, ptrs["ecdsa_cert0"][1])
    setq(0x121420, ptrs["ecdsa_cert1"][0]); setq(0x121428, ptrs["ecdsa_cert1"][1])
    setq(0x121430, ptrs["ecdsa_cert2"][0]); setq(0x121438, ptrs["ecdsa_cert2"][1])
    setq(0x121440, ptrs["rsa_cert0"][0]);   setq(0x121448, ptrs["rsa_cert0"][1])
    setq(0x121450, ptrs["rsa_cert1"][0]);   setq(0x121458, ptrs["rsa_cert1"][1])
    setq(0x121460, ptrs["ecdsa_cert2"][0])  # rsa root = shared root
    return ptrs


def verify_lib(raw, kb, ptrs):
    from cryptography import x509
    for name, (off, ln) in ptrs.items():
        c = x509.load_der_x509_certificate(bytes(raw[off:off + ln]))
        want = x509.load_der_x509_certificate(kb[name])
        if c.serial_number != want.serial_number:
            die(f"verify: {name} serial sai tại 0x{off:x}")
    # untouched zones
    if bytes(raw[0x2B974:0x2B974 + 609]) == b"":
        die("verify: RSA-1024 key bị ghi đè?")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", help="vendor libsoft_attestation_cert.so (bản GỐC)")
    ap.add_argument("--super", help="super.img — patch in-place @0x82254000")
    ap.add_argument("--keybox", required=True)
    ap.add_argument("--out", help="file lib mới (mặc định <lib>.keybox)")
    args = ap.parse_args()
    if not args.lib and not args.super:
        die("cần --lib hoặc --super")

    kb = parse_keybox(args.keybox)
    print(f"[keybox_embed] keybox: ecdsa {len(kb['ecdsa_key'])}B key + "
          f"{[len(kb[f'ecdsa_cert{i}']) for i in range(3)]}B chain; "
          f"rsa {len(kb['rsa_key'])}B key + "
          f"{[len(kb[f'rsa_cert{i}']) for i in range(3)]}B chain")

    assert_layout(kb)
    if args.lib:
        raw = bytearray(open(args.lib, "rb").read())
        if len(raw) != LIB_SIZE:
            die(f"lib size lạ: {len(raw)} != {LIB_SIZE}")
        if hashlib.md5(raw).hexdigest() != ORIG_MD5:
            print("[keybox_embed] CẢNH BÁO: md5 khác lib gốc — patch tiếp tục")
        ptrs = patch_lib(raw, kb)
        verify_lib(raw, kb, ptrs)
        out = args.out or args.lib + ".keybox"
        open(out, "wb").write(bytes(raw))
        print(f"[keybox_embed] OK -> {out} "
              f"(md5 {hashlib.md5(bytes(raw)).hexdigest()}, {len(raw)}B)")
        print(f"[keybox_embed] layout: " + " ".join(
            f"{n}@0x{o:x}/{l}B" for n, (o, l) in ptrs.items()))
    else:
        with open(args.super, "r+b") as f:
            f.seek(0x82254000)
            head = f.read(8)
            if head[:4] != b"\x7fELF":
                die("không tìm thấy ELF @0x82254000 — super.img lạ?")
            f.seek(0x82254000)
            raw = bytearray(f.read(LIB_SIZE))
            ptrs = patch_lib(raw, kb)
            verify_lib(raw, kb, ptrs)
            f.seek(0x82254000)
            f.write(bytes(raw))
            f.seek(0x82254000)
            chk = f.read(LIB_SIZE)
            if hashlib.md5(chk).hexdigest() != hashlib.md5(bytes(raw)).hexdigest():
                die("ghi super.img xong verify md5 SAI")
        print(f"[keybox_embed] super.img patched in-place @0x82254000, "
              f"md5 lib mới {hashlib.md5(bytes(raw)).hexdigest()}")


if __name__ == "__main__":
    main()
