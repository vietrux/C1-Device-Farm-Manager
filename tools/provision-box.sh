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

rc=0
for b in $BOXES; do
  echo "================ $b ================"
  # dòng đầu stdin = mật khẩu cho sudo -S, phần còn lại = script cho sh -s
  { printf '%s\n' "$BOX_SUDO_PASS"; cat "$HOST_SCRIPT"; } \
    | ssh -o ConnectTimeout=10 "$b" "sudo -S -p '' env $ENVS sh -s -- ${ARGS[*]}" \
    || rc=$?
  echo
done
exit $rc
