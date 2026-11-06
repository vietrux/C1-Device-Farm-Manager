#!/usr/bin/env bash
# provision-box.sh — chạy tools/box/df-host.sh trên một hoặc nhiều box RK3588 từ laptop.
#
# Script được đẩy qua stdin (sh -s) nên không để lại file tạm trên box.
# sudo trên box hỏi mật khẩu 1 lần ở laptop (hoặc lấy từ biến BOX_SUDO_PASS).
#
# Dùng:
#   ./provision-box.sh user@<ip-box>                  # check (chỉ đọc)
#   ./provision-box.sh user@<ip-box> apply
#   ./provision-box.sh user@10.16.68.86 apply --takeover    # box còn chạy hệ vendor
#   ./provision-box.sh user@<ip-box> revert
#   ./provision-box.sh "user@box1 user@box2" check          # nhiều box
#   ./provision-box.sh user@box --copy-id                    # cài SSH key laptop lên box trước
#
# Biến DF_* (xem đầu df-host.sh) được chuyển tiếp sang box, vd:
#   DF_DOCKER_TCP=127.0.0.1:2375 ./provision-box.sh user@box apply
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOST_SCRIPT="$HERE/box/df-host.sh"
[ -f "$HOST_SCRIPT" ] || { echo "LỖI: thiếu $HOST_SCRIPT" >&2; exit 2; }
[ $# -ge 1 ] || { sed -n '2,18p' "$0"; exit 2; }

BOXES="$1"; shift
if [ "${1:-}" = "--copy-id" ]; then
  for b in $BOXES; do ssh-copy-id "$b"; done
  exit 0
fi
ARGS=("${@:-check}")

# chuyển tiếp các biến DF_* sang box (giá trị được quote an toàn)
ENVS=""
while IFS='=' read -r k _; do
  ENVS+="$k=$(printf '%q' "${!k}") "
done < <(env | grep -E '^DF_[A-Z_]+=' || true)

if [ -z "${BOX_SUDO_PASS:-}" ]; then
  read -rsp "Mật khẩu sudo trên box: " BOX_SUDO_PASS; echo
fi

# resetprop cho df_agent (RESETPROP_SRC=/opt/df/resetprop) — agent cần nó để áp
# 77 props hồ sơ + IMEI lúc máy vừa boot; thiếu là máy lộ fingerprint rockchip thô
# (Google chặn như box mới tinh chưa fix). File = binary magisk đổi tên (applet theo argv[0]).
RESET_SRC="${DF_RESETPROP_SRC:-$HOME/.cache/devicefarm/integrity/magisk/resetprop}"
install_resetprop() {  # $1 = user@box
  local b="$1"
  if { printf '%s\n' "$BOX_SUDO_PASS"; } \
     | ssh -o ConnectTimeout=10 "$b" "sudo -S -p '' test -x /opt/df/resetprop" 2>/dev/null; then
    echo "  resetprop: đã có sẵn trên box"; return 0
  fi
  [ -f "$RESET_SRC" ] || { echo "  ⚠ resetprop: thiếu $RESET_SRC — chạy ./tools/integrity/fetch-assets.sh rồi provision lại, hoặc copy /opt/df/resetprop từ box khác. Máy sẽ KHÔNG có hồ sơ thiết bị nếu thiếu!"; return 1; }
  { printf '%s\n' "$BOX_SUDO_PASS"; cat "$RESET_SRC"; } \
    | ssh -o ConnectTimeout=10 "$b" "sudo -S -p '' sh -c 'mkdir -p /opt/df && cat > /opt/df/resetprop && chmod 755 /opt/df/resetprop'" \
    && echo "  resetprop: đã cài /opt/df/resetprop ($(md5sum <"$RESET_SRC" | cut -c1-8))" \
    || { echo "  ⚠ resetprop: cài thất bại"; return 1; }
}

rc=0
for b in $BOXES; do
  echo "================ $b ================"
  # cài resetprop TRƯỚC khi apply (apply tạo máy cần nó ngay)
  for a in "${ARGS[@]}"; do [ "$a" = apply ] && install_resetprop "$b" || true; done
  # dòng đầu stdin = mật khẩu cho sudo -S, phần còn lại = script cho sh -s
  { printf '%s\n' "$BOX_SUDO_PASS"; cat "$HOST_SCRIPT"; } \
    | ssh -o ConnectTimeout=10 "$b" "sudo -S -p '' env $ENVS sh -s -- ${ARGS[*]}" \
    || rc=$?
  echo
done
exit $rc
