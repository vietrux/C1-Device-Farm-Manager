#!/usr/bin/env bash
# fetch-assets.sh — download + verify the integrity-fix binaries into a cache.
#
# These are third-party binaries (Magisk, Play Integrity Fork) with pinned
# versions and sha256, so they are NOT committed to the repo. Run once on the
# console machine; `console/integrity.py` reads the cache it populates.
#
#   ./fetch-assets.sh [DEST]      # default DEST: ~/.cache/devicefarm/integrity
#
# Layout produced:
#   $DEST/magisk/      magisk, magiskboot, magiskinit, magiskpolicy, busybox,
#                      init-ld, *.sh (renamed from the Magisk APK's lib/arm64)
#   $DEST/pif.zip      Play Integrity Fork module
#   $DEST/keyattestation/KeyAttestation.apk   app kiểm tra attestation (tự cài)
#   $DEST/MANIFEST     versions + sha256 of what was fetched
set -euo pipefail

MAGISK_VER="v31.1"
MAGISK_URL="https://github.com/topjohnwu/Magisk/releases/download/${MAGISK_VER}/Magisk-${MAGISK_VER}.apk"
MAGISK_SHA="89cefac0a318afa39fb3880c45c87b036c05bc1ebf02a7a715b7f8d7c26ac6e7"

PIF_VER="v19"
PIF_URL="https://github.com/osm0sis/PlayIntegrityFork/releases/download/${PIF_VER}/PlayIntegrityFork-${PIF_VER}.zip"
PIF_SHA="83b4d5f32743805c5b5a69361d73b8ab9c095325e24f20711afa1abea837b58f"

# Key Attestation (vvb2060) — app kiểm tra chuỗi attestation của keymaster
# (cho biết keybox có được Google chứng nhận không). Integrity-fix tự cài lên máy.
KA_VER="v1.8.4"
KA_URL="https://github.com/vvb2060/KeyAttestation/releases/download/${KA_VER}/KeyAttestation-${KA_VER}.apk"
KA_SHA="e22ae305deca589fc9af2dc8940f5c62bac7120bed817f80da1f345a9ec69897"

DEST="${1:-${XDG_CACHE_HOME:-$HOME/.cache}/devicefarm/integrity}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

verify() { echo "$2  $1" | sha256sum -c - >/dev/null 2>&1 || { echo "sha256 mismatch: $1" >&2; exit 1; }; }

echo "fetching into $DEST"
mkdir -p "$DEST/magisk"

echo "- Magisk ${MAGISK_VER}"
curl -fsSL -o "$TMP/magisk.apk" "$MAGISK_URL"
verify "$TMP/magisk.apk" "$MAGISK_SHA"

echo "- Play Integrity Fork ${PIF_VER}"
curl -fsSL -o "$DEST/pif.zip" "$PIF_URL"
verify "$DEST/pif.zip" "$PIF_SHA"

echo "- Key Attestation ${KA_VER}"
mkdir -p "$DEST/keyattestation"
curl -fsSL -o "$DEST/keyattestation/KeyAttestation.apk" "$KA_URL"
verify "$DEST/keyattestation/KeyAttestation.apk" "$KA_SHA"

# Extract Magisk arm64 binaries, renamed from their lib*.so packaging names.
python3 - "$TMP/magisk.apk" "$DEST/magisk" <<'PY'
import sys, zipfile, posixpath
apk, out = sys.argv[1], sys.argv[2]
RENAME = {"libmagisk.so":"magisk","libmagisk64.so":"magisk64","libmagiskinit.so":"magiskinit",
          "libmagiskpolicy.so":"magiskpolicy","libbusybox.so":"busybox","libmagiskboot.so":"magiskboot",
          "libinit-ld.so":"init-ld"}
with zipfile.ZipFile(apk) as z:
    for n in z.namelist():
        if n.endswith("/"): continue
        d, base = posixpath.dirname(n), posixpath.basename(n)
        if d == "lib/arm64-v8a":
            open(f"{out}/{RENAME.get(base, base)}", "wb").write(z.read(n))
        elif d == "assets" and (base.endswith(".sh") or base == "stub.apk"):
            open(f"{out}/{base}", "wb").write(z.read(n))
PY

# Ship the device launcher + conf alongside the binaries so the console pushes
# one directory.
HERE="$(cd "$(dirname "$0")" && pwd)"
cp "$HERE/device/farm-magisk.sh" "$DEST/magisk/farm-magisk.sh"
# resetprop = binary magisk đổi tên (multi-call applet theo argv[0]) — df_agent
# copy sang /opt/df/resetprop trên box để áp hồ sơ thiết bị lúc máy vừa boot;
# provision-box.sh tự cài lên box khi apply.
cp "$DEST/magisk/magisk" "$DEST/magisk/resetprop"
cat > "$DEST/magisk/farm-magisk.conf" <<'CONF'
ZYGISK=1
MAGISK_DAEMON=magisk
MAGISKTMP=/debug_ramdisk
CONF

{
    echo "magisk ${MAGISK_VER} ${MAGISK_SHA}"
    echo "pif ${PIF_VER} ${PIF_SHA}"
    echo "keyattestation ${KA_VER} ${KA_SHA}"
    echo "fetched $(date -u +%FT%TZ)"
} > "$DEST/MANIFEST"

echo "done. $(ls "$DEST/magisk" | wc -l) magisk files + pif.zip in $DEST"
