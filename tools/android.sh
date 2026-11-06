#!/usr/bin/env bash
# android.sh — mở màn hình điều khiển một máy trong RK3588 device farm bằng scrcpy.
#
# Hệ của mình: host RK3588 (SSH qua Tailscale: user@<ip-box>). Container Android
# chạy trên docker BRIDGE (172.30.0.0/24) nên IP container chỉ thấy được từ chính host.
# Laptop đi qua SSH tunnel:  127.0.0.1:<15000+octet cuối>  ->  <ip container>:5555
# (cần 'AllowTcpForwarding local' trong sshd_config của host). Chạy được ở mọi mạng,
# không đụng IP của LAN công ty.
# Image để ro.adb.secure=1 → public key adb của laptop (~/.android/adbkey.pub) phải có
# trong <data>/misc/adb/adb_keys của container.
#
# Dùng:
#   ./android.sh                  # máy mặc định (df-v1)
#   ./android.sh df-2             # theo tên container (tra IP qua host)
#   ./android.sh 172.30.0.52      # theo IP container
#   ./android.sh list             # liệt kê máy trong farm
#   ./android.sh df-v1 --max-fps 60   # cờ sau cùng được chuyển thẳng cho scrcpy
# Mật khẩu sudo trên box: đặt BOX_SUDO_PASS, không thì script hỏi.
set -euo pipefail

# ------------------------------- cấu hình -------------------------------
BOX="${FARM_BOX:-user@<ip-box>}"    # host device farm
DEFAULT_DEV="${FARM_DEV:-df-v1}"          # máy mặc định khi không truyền tham số
ADB_PORT="${FARM_ADB_PORT:-5555}"         # cổng adbd trong container
PORT_BASE="${FARM_PORT_BASE:-15000}"      # cổng local = PORT_BASE + octet cuối của IP
HERE="$(cd "$(dirname "$0")" && pwd)"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8)

# scrcpy: ưu tiên bản bundle cạnh script (như bản Jetson), rồi tới bản hệ thống
SCRCPY=""
for c in "$HERE"/scrcpy/scrcpy "$HERE"/scrcpy/scrcpy-linux-* ; do
  [ -x "$c" ] && { SCRCPY="$c"; break; }
done
[ -z "$SCRCPY" ] && SCRCPY="$(command -v scrcpy || true)"

die(){ echo "LỖI: $*" >&2; exit 1; }
# chạy một đoạn shell trên box với quyền root. Dòng đầu stdin là mật khẩu cho sudo -S,
# phần còn lại là script cho sh -s (cùng cách với provision-box.sh, không lo quote lồng nhau).
# Mật khẩu lấy từ BOX_SUDO_PASS, không có thì hỏi 1 lần.
box_sh(){
  if [ -z "${BOX_SUDO_PASS:-}" ]; then read -rsp "Mật khẩu sudo trên box: " BOX_SUDO_PASS; echo >&2; fi
  printf '%s\n%s\n' "$BOX_SUDO_PASS" "$1" | ssh "${SSH_OPTS[@]}" "$BOX" "sudo -S -p '' sh -s"
}
command -v adb >/dev/null || die "laptop chưa có adb  (sudo apt install adb)"

# ------------------------------- list -----------------------------------
if [ "${1:-}" = "list" ]; then
  echo "Máy đang chạy trong farm (hỏi $BOX):"
  box_sh "ids=\$(docker ps -aq --filter name=df-); [ -z \"\$ids\" ] || docker inspect -f '{{.Name}}	{{.State.Status}}	{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \$ids" \
    | sed 's|^/||' || die "không hỏi được $BOX"
  exit 0
fi

# kiểm scrcpy sớm (trừ 'list') để khỏi bắt chờ boot rồi mới báo thiếu
[ -n "$SCRCPY" ] && [ -x "$SCRCPY" ] \
  || die "chưa có scrcpy  (sudo apt install scrcpy  |  snap install scrcpy  |  đặt binary vào $HERE/scrcpy/)"

# -------------------- xác định IP container -----------------------------
ARG="${1:-$DEFAULT_DEV}"
if [[ "$ARG" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  DEV_IP="$ARG"
else
  echo "Tra IP cho '$ARG' qua $BOX ..."
  DEV_IP="$(box_sh "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' '$ARG' 2>/dev/null" | tr -d '\r')"
  [ -n "$DEV_IP" ] || die "không tra được IP cho '$ARG'  (thử: $(basename "$0") list)"
fi
LPORT=$((PORT_BASE + ${DEV_IP##*.}))
TARGET="127.0.0.1:$LPORT"
echo "Máy: $ARG  ->  $DEV_IP:$ADB_PORT  (tunnel $TARGET)"

# -------------------- SSH tunnel (dùng lại nếu đã mở) -------------------
listening(){ ss -ltn 2>/dev/null | grep -q "127.0.0.1:$LPORT "; }
if ! listening; then
  ssh "${SSH_OPTS[@]}" -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \
    -f -N -L "$LPORT:$DEV_IP:$ADB_PORT" "$BOX" \
    || die "không mở được tunnel (host có 'AllowTcpForwarding local' chưa?)"
  for _ in 1 2 3 4 5; do listening && break; sleep 0.5; done
fi
listening || die "tunnel không lên ở $TARGET"

# -------------------- adb connect (retry, KHÔNG kill-server) ------------
# chỉ disconnect/reconnect đúng target này, không 'adb kill-server'
# để khỏi rớt các phiên adb khác bạn đang mở.
state(){ adb devices 2>/dev/null | awk -v t="$TARGET" '$1==t{print $2}'; }
timeout 10 adb connect "$TARGET" >/dev/null 2>&1 || true
if [ "$(state)" != "device" ]; then
  adb disconnect "$TARGET" >/dev/null 2>&1 || true
  sleep 0.5
  timeout 10 adb connect "$TARGET" >/dev/null 2>&1 || true
fi
case "$(state)" in
  device) ;;
  unauthorized) die "$TARGET unauthorized: thêm ~/.android/adbkey.pub vào <data>/misc/adb/adb_keys của container" ;;
  *) echo "Hiện trạng adb:"; adb devices; die "không nối được $TARGET (container đã boot chưa?)" ;;
esac

# chờ Android boot xong để scrcpy không mở vào màn hình đen.
# Dò nhiều cờ vì có image KHÔNG set sys.boot_completed:
#   sẵn sàng khi sys.boot_completed=1  HOẶC  dev.bootcomplete=1  HOẶC  bootanim=stopped.
ready(){
  local bc dbc ba
  bc=$(timeout 8 adb -s "$TARGET" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
  dbc=$(timeout 8 adb -s "$TARGET" shell getprop dev.bootcomplete 2>/dev/null | tr -d '\r')
  ba=$(timeout 8 adb -s "$TARGET" shell getprop init.svc.bootanim 2>/dev/null | tr -d '\r')
  [ "$bc" = "1" ] || [ "$dbc" = "1" ] || [ "$ba" = "stopped" ]
}
echo -n "Chờ boot"
for _ in $(seq 1 60); do ready && { echo " OK"; break; }; echo -n "."; sleep 1; done

# ------------------------------- scrcpy ---------------------------------
echo "Mở scrcpy ($SCRCPY) ..."
exec env ADB="$(command -v adb)" "$SCRCPY" -s "$TARGET" \
  --window-title "farm $ARG @ $DEV_IP" \
  --no-audio --max-size 720 --video-bit-rate 2M --max-fps 30 "${@:2}"
