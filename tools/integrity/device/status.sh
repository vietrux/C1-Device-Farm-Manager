#!/system/bin/sh
# status.sh — báo trạng thái integrity-fix của container. In MỘT object JSON.
# Chạy với root qua `adb shell`. Không bao giờ fail (luôn in JSON).
#
# 2026-11-06: đo lại theo cơ chế ĐÃ CHỨNG MINH:
#  - DEVICE integrity = chuỗi keybox NHÚNG trong /vendor/lib64/libsoft_attestation_cert.so
#    (HAL không bao giờ đọc /data/system/keymaster.xml — chỉ báo cáo để đối chiếu legacy).
#  - BASIC = props thiết bị (MYTPIF dựng sẵn + hồ sơ resetprop) — báo fingerprint thật +
#    trạng thái bootloader giả.
#  - Magisk/Zygisk/PIF module không tải được trong container (chỉ báo cáo có/không).
set -u
MAGISKTMP=/debug_ramdisk

# ---- chuỗi keybox đang được serve (nguồn sự thật của DEVICE) ----
chain_lib_md5=$(md5sum /vendor/lib64/libsoft_attestation_cert.so 2>/dev/null | cut -d' ' -f1)

# ---- props thật của máy (nguồn BASIC) ----
prop_fingerprint=$(getprop ro.build.fingerprint)
flash_locked=$(getprop ro.boot.flash.locked)
vbmeta_state=$(getprop ro.boot.vbmeta.device_state)

# ---- legacy: magisk/zygisk/PIF (không ảnh hưởng PI trong container) ----
magisk_ver=""
[ -x "$MAGISKTMP/magisk" ] && magisk_ver=$(timeout 6 "$MAGISKTMP/magisk" -v 2>/dev/null)
daemon=false
pgrep -f magiskd >/dev/null 2>&1 && daemon=true
zygisk=""
[ -x "$MAGISKTMP/magisk" ] && zygisk=$(timeout 6 "$MAGISKTMP/magisk" --sqlite "SELECT value FROM settings WHERE key='zygisk';" 2>/dev/null | sed 's/value=//')
pif_module=false
[ -d /data/adb/modules/playintegrityfix ] && pif_module=true
pif_fingerprint=$(grep -m1 '^FINGERPRINT=' /data/adb/modules/playintegrityfix/custom.pif.prop 2>/dev/null | cut -d= -f2-)

# ---- legacy: keymaster.xml (cơ chế CHẾT — HAL không đọc; chỉ để đối chiếu) ----
keymaster_xml=false
[ -f /data/system/keymaster.xml ] && keymaster_xml=true
keymaster_xml_device=$(grep -o 'DeviceID="[^"]*"' /data/system/keymaster.xml 2>/dev/null | head -1 | sed 's/DeviceID="//;s/"//')

# ---- app đo lường ----
# pm có thể chưa sống lại ngay sau apply (restart zygote) — chờ tí để khỏi báo sai
i=0
while [ $i -lt 20 ] && ! pm path android >/dev/null 2>&1; do sleep 2; i=$((i+1)); done
keyattestation=false
pm path io.github.vvb2060.keyattestation >/dev/null 2>&1 && keyattestation=true
spic=false
pm path com.henrikherzig.playintegritychecker >/dev/null 2>&1 && spic=true

printf '{"chain_lib_md5":"%s","prop_fingerprint":"%s","flash_locked":"%s","vbmeta_state":"%s","magisk":"%s","daemon":%s,"zygisk":"%s","pif_module":%s,"pif_fingerprint":"%s","keymaster_xml":%s,"keymaster_xml_device":"%s","keyattestation":%s,"spic":%s}\n' \
    "${chain_lib_md5}" "${prop_fingerprint}" "${flash_locked}" "${vbmeta_state}" \
    "${magisk_ver}" "$daemon" "${zygisk:-}" "$pif_module" "${pif_fingerprint}" \
    "$keymaster_xml" "${keymaster_xml_device}" "$keyattestation" "$spic"
