#!/usr/bin/env bash
# farm-console.sh — mở web quản lý farm như một app.
#
#   ./tools/farm-console.sh              bật: cổng ngẫu nhiên còn trống, tự mở trình duyệt.
#                                        Tắt: đóng tab (tự tắt sau ~3 phút) hoặc Ctrl+C.
#   ./tools/farm-console.sh --install    thêm biểu tượng "farm-console" vào menu ứng dụng
#   ./tools/farm-console.sh --uninstall  gỡ biểu tượng
#   ./tools/farm-console.sh --rebuild    build lại giao diện web rồi bật
#   ./tools/farm-console.sh -- <cờ>      cờ khác chuyển cho farm-console (xem: -- --help)
#
# Lần chạy đầu tự cài môi trường Python (.venv) và build giao diện (cần uv hoặc python3, và node/npm).
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APP="$HERE/../farm-console"
APP="$(cd "$APP" && pwd)"
DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/farm-console.desktop"
die(){ echo "LỖI: $*" >&2; exit 1; }

case "${1:-}" in
  --install)
    mkdir -p "$(dirname "$DESKTOP")"
    cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=farm-console
Comment=Quản lý box RK3588 và máy Android
Exec=$HERE/farm-console.sh
Icon=utilities-system-monitor
Terminal=false
Categories=Development;System;
EOF
    chmod +x "$DESKTOP"
    echo "Đã thêm farm-console vào menu ứng dụng ($DESKTOP)."
    exit 0 ;;
  --uninstall)
    rm -f "$DESKTOP" && echo "Đã gỡ $DESKTOP"; exit 0 ;;
esac

REBUILD=0
[ "${1:-}" = "--rebuild" ] && { REBUILD=1; shift; }
[ "${1:-}" = "--" ] && shift

# ---- môi trường Python
if [ ! -x "$APP/.venv/bin/farm-console" ]; then
  echo "Cài môi trường Python cho farm-console (lần đầu)…"
  if command -v uv >/dev/null; then
    uv venv -q "$APP/.venv" && uv pip install -q --python "$APP/.venv/bin/python" -e "$APP"
  else
    python3 -m venv "$APP/.venv" && "$APP/.venv/bin/pip" install -q -e "$APP"
  fi
fi

# ---- giao diện web (build tĩnh vào web/out)
if [ $REBUILD = 1 ] || [ ! -f "$APP/web/out/index.html" ]; then
  command -v npm >/dev/null || die "cần node/npm để build giao diện (một lần)"
  echo "Build giao diện web…"
  ( cd "$APP/web" && { [ -d node_modules ] || npm ci --silent; } && npm run -s build >/dev/null )
fi

# Mở từ menu ứng dụng (không có terminal): ghi log ra file để còn xem được khi lỗi
if [ ! -t 1 ]; then
  LOG="${XDG_STATE_HOME:-$HOME/.local/state}/farm-console.log"
  mkdir -p "$(dirname "$LOG")"
  exec "$APP/.venv/bin/farm-console" "$@" >>"$LOG" 2>&1
fi
exec "$APP/.venv/bin/farm-console" "$@"
