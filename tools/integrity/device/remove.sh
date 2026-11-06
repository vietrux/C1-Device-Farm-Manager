#!/system/bin/sh
# remove.sh — DỌN DI SẢN integrity (không đổi chain nhúng).
#
# Chạy root qua `adb shell`. Cơ chế thật 2026-11-06: chain DEVICE nằm trong lib
# vendor NHÚNG (image layer) — file keymaster.xml KHÔNG được HAL đọc, module
# Magisk/PIF không tải trong container. Script này chỉ gỡ artefact legacy cho
# status sạch mắt: Magisk + modules, keymaster.xml, rồi restart zygote/HAL.
# In ra "REMOVE_OK". Muốn đổi chain → xoay keybox (integrity apply).
set -u
MAGISKTMP=/debug_ramdisk

# 1. Stop the Magisk daemon and tear down its tmpfs.
[ -x "$MAGISKTMP/magisk" ] && "$MAGISKTMP/magisk" --stop 2>/dev/null
pkill -f magiskd 2>/dev/null
pkill -f zygiskd 2>/dev/null
umount "$MAGISKTMP" 2>/dev/null

# 2. Remove all on-device Magisk state (modules, db, binaries, leftovers).
rm -rf /data/adb/modules /data/adb/modules_update /data/adb/magisk \
       /data/adb/magisk.db /data/adb/post-fs-data.d /data/adb/service.d \
       /data/adb/zygisksu /data/adb/tricky_store /data/adb/zygisk 2>/dev/null

# 3. Xoá keymaster.xml (di sản — HAL không đọc, chỉ để status sạch);
#    restart HAL cho đồng bộ (chain nhúng vẫn nguyên).
rm -f /data/system/keymaster.xml 2>/dev/null
for svc in vendor.keymaster-4-0 vendor.keymaster-4-1 vendor.keymint-default keystore2; do
    setprop ctl.restart "$svc" 2>/dev/null
done

# 4. Reload zygote để hook zygisk/PIF (nếu có) sạch hẳn.
setprop ctl.restart zygote 2>/dev/null

echo "status: magisk=$([ -e /data/adb/magisk ] && echo present || echo gone) keymaster=$([ -f /data/system/keymaster.xml ] && echo present || echo gone)"
echo "REMOVE_OK"
