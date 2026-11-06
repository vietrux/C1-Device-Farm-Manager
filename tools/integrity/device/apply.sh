#!/system/bin/sh
# apply.sh — turn on integrity-fix inside a running dobox Q12 container.
#
# Run as root via `adb shell`. Assets must already be pushed by the console:
#   /data/adb/magisk/           magisk binaries + farm-magisk.sh + farm-magisk.conf
#   /data/local/tmp/farm-pif.zip   Play Integrity Fork module zip
#   /data/local/tmp/farm-keybox.xml (optional) keybox to install via the vendor
#                                   extservice; skipped if absent
#
# Prints "APPLY_OK" on success (plus a short status line), or "APPLY_FAIL: ..".
# Idempotent: re-running refreshes the fingerprint and re-uploads the keybox.
set -u
MAGISKTMP=/debug_ramdisk
PIF_ZIP=/data/local/tmp/farm-pif.zip
KEYBOX=/data/local/tmp/farm-keybox.xml
KA_APK=/data/local/tmp/KeyAttestation.apk
EXT_PORT="${EXT_PORT:-9082}"

fail() { echo "APPLY_FAIL: $*"; exit 1; }

[ "$(id -u)" = "0" ] || fail "need root"
[ -f /data/adb/magisk/magisk ] || fail "magisk binaries not pushed"
[ -f /data/adb/magisk/farm-magisk.sh ] || fail "farm-magisk.sh not pushed"

chmod 0755 /data/adb/magisk/magisk* /data/adb/magisk/busybox \
           /data/adb/magisk/init-ld /data/adb/magisk/*.sh 2>/dev/null
mkdir -p /data/adb/modules

# 1. Magisk daemon + zygisk (post-fs-data phase). Guard with a timeout: a broken
#    environment makes --daemon spin (see farm-magisk.sh header).
timeout 40 sh /data/adb/magisk/farm-magisk.sh post-fs-data || fail "post-fs-data phase failed/timeout"

MAGISK="$MAGISKTMP/magisk"
[ -x "$MAGISK" ] || MAGISK=/data/adb/magisk/magisk
VER=$(timeout 8 "$MAGISK" -v 2>/dev/null)
case "$VER" in
    *MAGISK*) : ;;
    *) fail "magisk daemon not responding (got: ${VER:-nothing})" ;;
esac

# 2. Play Integrity Fork module.
if [ -f "$PIF_ZIP" ]; then
    timeout 60 "$MAGISK" --install-module "$PIF_ZIP" >/dev/null 2>&1 \
        || fail "PIF module install failed"
    # Fresh Pixel "Canary" fingerprint. autopif4 needs busybox at the magisk path.
    if [ -f /data/adb/modules/playintegrityfix/autopif4.sh ]; then
        ( cd /data/adb/modules/playintegrityfix && timeout 90 sh autopif4.sh >/dev/null 2>&1 ) \
            || echo "warn: autopif4 fingerprint refresh failed (module still active)"
    fi
fi

# 3. boot-complete phase (re-assert zygisk) + reload zygote so zygisk injects.
timeout 30 sh /data/adb/magisk/farm-magisk.sh boot-complete >/dev/null 2>&1

# 4. Keybox via the vendor extservice (official path: writes /data/system/
#    keymaster.xml, restarts keymaster HALs + keystore2, clears GMS).
KB_STATUS="skipped"
if [ -f "$KEYBOX" ]; then
    RESP=$(timeout 30 /system/xbin/curl -sS \
        -F "name=farm-keybox.xml" \
        -F "file=@$KEYBOX;filename=farm-keybox.xml;type=text/xml" \
        "http://127.0.0.1:${EXT_PORT}/uploadkeybox" 2>&1)
    case "$RESP" in
        *permissionabc*selinux123*|*导入完成*) KB_STATUS="installed" ;;
        *) KB_STATUS="error:${RESP}" ;;
    esac
    rm -f "$KEYBOX"
fi

# 5. Key Attestation app (vvb2060) — xem keybox/attestation có được Google tin.
# Cài TRƯỚC khi restart zygote: ngay sau zygote restart, system_server (con của
# zygote) chưa nhận install session dù `pm path` đã trả lời. Idempotent: -r
# thay thế bản cũ; thiếu APK thì bỏ qua (status ghi "absent").
KA_STATUS="absent"
if [ -f "$KA_APK" ]; then
    if pm install -r "$KA_APK" >/dev/null 2>&1; then
        KA_STATUS="installed"
    else
        KA_STATUS="error"
    fi
    rm -f "$KA_APK"
fi

# 6. Reload zygote (zygisk + PIF take effect) and nudge GMS.
setprop ctl.restart zygote 2>/dev/null

ZYGISK=$(timeout 8 "$MAGISK" --sqlite "SELECT value FROM settings WHERE key='zygisk';" 2>/dev/null)
echo "status: magisk=${VER%% *} zygisk=${ZYGISK:-?} pif=$([ -d /data/adb/modules/playintegrityfix ] && echo on || echo off) keybox=$KB_STATUS keyattestation=$KA_STATUS"
case "$KB_STATUS" in
    error:*) fail "keybox upload: ${KB_STATUS#error:}" ;;
esac
echo "APPLY_OK"
