#!/system/bin/sh
# farm-magisk.sh — bootless Magisk launcher for the dobox Q12 image.
#
# The container has no boot image, so magiskinit never runs as PID 1 to set up
# Magisk. Instead the binaries live in /data/adb/magisk and this script performs
# the phases magiskinit would normally trigger. Called by apply.sh (not by init;
# the image's partitions are read-only+full so we cannot drop an .rc hook).
#
# Phases: post-fs-data | service | boot-complete | zygote-restart
#
# IMPORTANT (hard-won): the daemon needs $MAGISKTMP/.magisk/device to exist
# BEFORE it starts, otherwise it spins forever trying to mkfifo its log/socket
# there and the --daemon call hangs. setup_tmp creates it.
SRC=/data/adb/magisk
ZYGISK=1
MAGISKTMP=/debug_ramdisk
[ -f "$SRC/farm-magisk.conf" ] && . "$SRC/farm-magisk.conf"

say() { log -t farm-magisk "$*" 2>/dev/null; echo "farm-magisk: $*"; }

setup_tmp() {
    [ -d "$MAGISKTMP" ] || mkdir -p "$MAGISKTMP"
    if ! mountpoint -q "$MAGISKTMP" 2>/dev/null; then
        mount -t tmpfs -o mode=0755 magisk "$MAGISKTMP" || say "tmpfs mount on $MAGISKTMP failed"
    fi
    for f in "$SRC"/*; do
        case "${f##*/}" in
            farm-magisk.sh|farm-magisk.conf) ;;
            *) cp -af "$f" "$MAGISKTMP/" ;;
        esac
    done
    chmod 0755 "$MAGISKTMP" "$MAGISKTMP"/magisk* "$MAGISKTMP"/busybox 2>/dev/null
    ln -sf ./magisk "$MAGISKTMP/su"
    ln -sf ./magisk "$MAGISKTMP/resetprop"
    # .magisk/device is REQUIRED before the daemon starts (see header note).
    mkdir -p "$MAGISKTMP/.magisk/device" "$MAGISKTMP/.magisk/mirror" \
             "$MAGISKTMP/.magisk/block" "$MAGISKTMP/.magisk/worker"
}

enable_zygisk() {
    [ "$ZYGISK" = "1" ] || return 0
    "$MAGISKTMP/magisk" --sqlite "REPLACE INTO settings (key,value) VALUES('zygisk',1);" \
        || say "could not enable zygisk via sqlite"
}

case "$1" in
    post-fs-data)
        setup_tmp
        "$MAGISKTMP/magiskpolicy" --live --magisk 2>/dev/null || say "magiskpolicy --live failed"
        "$MAGISKTMP/magisk" --daemon 2>/dev/null
        enable_zygisk
        "$MAGISKTMP/magisk" --post-fs-data 2>/dev/null
        ;;
    service)
        "$MAGISKTMP/magisk" --service 2>/dev/null
        ;;
    boot-complete)
        enable_zygisk
        "$MAGISKTMP/magisk" --boot-complete 2>/dev/null
        ;;
    zygote-restart)
        "$MAGISKTMP/magisk" --zygote-restart 2>/dev/null
        ;;
    *)
        echo "usage: $0 post-fs-data|service|boot-complete|zygote-restart" >&2
        exit 2
        ;;
esac
