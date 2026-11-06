#!/usr/bin/env bash
# farm-image.sh — kho image farm trên laptop + 1 lệnh đẩy lên box mới.
#
#   ./farm-image.sh pull <tag-đầy-đủ> <user@box>     kéo image từ box về kho
#                                                     (~/.cache/devicefarm/images/)
#   ./farm-image.sh push <tag-hoặc-alias> <user@box> đẩy kho sang box mới + VERIFY
#   ./farm-image.sh list                             xem kho
#
# Alias: sau lần pull đầu, gọi được tên ngắn (push freebee …) — manifest tự map.
#
# Sau khi load trên box mới, script ĐỌC LẠI lib @0x82254000 trong layer của
# image vừa load và so md5 với lúc pull → chứng minh chuỗi keybox nhúng
# nguyên vẹn trên máy mới (không chỉ "load xong" mà là "chain đúng").
#
# sudo trên box dùng password vendor mặc định 'myt' — ghi đè bằng:
#   SUDO_PW=khac ./farm-image.sh push ...
#
# Lưu ý: image xoay keybox bằng dd thẳng vào layer (công thức xoay runtime)
# sẽ vỡ checksum → KHÔNG save được. Pull từ image bake chuẩn (Dockerfile).
set -euo pipefail

ARCHIVE="${XDG_CACHE_HOME:-$HOME/.cache}/devicefarm/images"
MANIFEST="$ARCHIVE/manifest.json"
# Mật khẩu sudo của box — KHÔNG có mặc định (không công bố default vendor
# trên repo public). Đặt biến môi trường: SUDO_PW=... ./farm-image.sh push …
SUDO_PW="${SUDO_PW:-}"
# 0x82254000/4096 = 533076 — LUÔN tính bằng python, đừng nhẩm (bài học 1.7MB)
SEEK=533076
BSIZE=1183168    # libsoft_attestation_cert.so

die(){ echo "LỖI: $*" >&2; exit 1; }
[ -n "$SUDO_PW" ] || die "thiếu SUDO_PW — chạy: SUDO_PW=<mật-khẩu-sudo-box> $0 …"
need(){ command -v "$1" >/dev/null || die "thiếu $1 trên laptop"; }
need ssh; need python3; need gzip

mkdir -p "$ARCHIVE"

_resolve(){                       # $1: tag-đầy-đủ hoặc alias → in tag đầy đủ
  local want="$1" rc
  if [[ "$want" == *:* ]]; then echo "$want"; return; fi
  set +e
  python3 - "$MANIFEST" "$want" <<'PY'
import json, sys
try:
    m = json.load(open(sys.argv[1]))
except Exception:
    m = {}
hits = [k for k in m if k.split(":", 1)[-1].endswith(sys.argv[2])]
if len(hits) == 1:
    print(hits[0]); sys.exit(0)
sys.exit(2 if hits else 1)
PY
  rc=$?
  set -e
  [ $rc -eq 0 ] && return 0
  [ $rc -eq 2 ] && die "alias '$want' mơ hồ — dùng tag đầy đủ"
  die "không có '$want' trong kho — pull trước: ./farm-image.sh pull dobox:<tag> <user@box>"
}

_file_for(){ echo "$ARCHIVE/$(echo "$1" | tr ':/' '--').tar.gz"; }

# md5 lib chain @0x82254000 đọc TỪ LAYER của image trên box cho trước
_chain_md5_on(){
  local host="$1" img="$2" script out
  # heredoc KHÔNG quote → $SEEK/$BSIZE thay bằng giá trị cục bộ; biến remote
  # ghi \$ để giữ nguyên dấu $ khi script tới tay shell phía box
  script=$(cat <<REMOTE
L=\$(docker inspect "\$FARMIMG" --format '{{.GraphDriver.Data.LowerDir}}' | tr ':' ' ')
SUPER=""
for d in \$L; do [ -f "\$d/super.img" ] && SUPER="\$d/super.img" && break; done
[ -n "\$SUPER" ] || { echo no-super; exit 1; }
dd if="\$SUPER" bs=4096 skip=$SEEK count=289 2>/dev/null | head -c $BSIZE | md5sum
REMOTE
)
  out=$( { printf '%s\n' "$SUDO_PW"; printf '%s\n' "$script"; } | \
    ssh -o ConnectTimeout=10 "$host" \
    "sudo -S -p '' env FARMIMG='$img' sh -s" 2>/dev/null | awk '{print $1}')
  echo "$out"
}

_manifest_write(){               # $1=tag $2=file $3=sha256 $4=chain_md5 $5=pulled_from
  python3 - "$MANIFEST" "$1" "$2" "$3" "$4" "$5" <<'PY'
import json, sys, time, os
p, tag, f, sha, md5, src = sys.argv[1:7]
try:
    m = json.load(open(p))
except Exception:
    m = {}
m[tag] = {"file": os.path.basename(f), "size": os.path.getsize(f), "sha256": sha,
          "chain_md5": md5, "pulled_from": src, "pulled_at": time.strftime("%Y-%m-%d %H:%M")}
json.dump(m, open(p, "w"), indent=1, ensure_ascii=False)
PY
}

cmd_pull(){
  local tag="$1" host="$2" f sha md5
  f=$(_file_for "$tag")
  echo "== pull $tag từ $host → $f"
  printf '%s\n' "$SUDO_PW" | ssh -o ConnectTimeout=10 "$host" \
    "sudo -S -p '' env FARMIMG='$tag' sh -c 'docker save \"\$FARMIMG\" | gzip -1'" \
    > "$f" 2>/dev/null
  [ -s "$f" ] || die "stream rỗng — image không có trên $host hoặc save lỗi (checksum layer vỡ?)"
  gzip -t "$f" || die "gzip hỏng sau khi pull"
  sha=$(sha256sum "$f" | awk '{print $1}')
  md5=$(_chain_md5_on "$host" "$tag")
  [ "${#md5}" -eq 32 ] || die "không đọc được md5 lib từ layer trên $host ($md5)"
  _manifest_write "$tag" "$f" "$sha" "$md5" "$host"
  echo "   xong: $(du -h "$f" | cut -f1) · sha256 ${sha:0:12}… · chain md5 $md5"
}

cmd_push(){
  local tag host f sha md5 got
  tag=$(_resolve "$1"); host="$2"
  f=$(_file_for "$tag")
  [ -f "$f" ] || die "thiếu $f trong kho"
  sha=$(python3 -c "import json;print(json.load(open('$MANIFEST'))['$tag']['sha256'])")
  md5=$(python3 -c "import json;print(json.load(open('$MANIFEST'))['$tag']['chain_md5'])")
  echo "== verify kho trước khi đẩy…"
  echo "$sha  $f" | sha256sum -c - >/dev/null || die "file kho không khớp sha256 — kéo lại (pull)"
  echo "== push $tag → $host"
  { printf '%s\n' "$SUDO_PW"; cat "$f"; } | ssh -o ConnectTimeout=10 "$host" \
    "sudo -S -p '' docker load" 2>/dev/null | grep -E "^Loaded image" || die "docker load trên $host thất bại"
  echo "== verify chain trên $host…"
  got=$(_chain_md5_on "$host" "$tag")
  [ "$got" = "$md5" ] || die "chain SAU KHI LOAD khác lúc pull ($got ≠ $md5) — đừng dùng image này!"
  echo "   OK: chain $md5 nguyên vẹn trên $host ✓"
}

cmd_list(){
  python3 - "$MANIFEST" <<'PY' || echo "(kho trống — pull image trước)"
import json, sys, os
try:
    m = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
print(f"{'TAG':<34}{'SIZE':>9}  {'CHAIN MD5 @0x82254000':<34}PULLED")
for k, v in m.items():
    size = f"{v['size'] / 2**30:.1f}G"
    print(f"{k:<34}{size:>9}  {v['chain_md5']:<34}{v.get('pulled_at', '?')}")
PY
}

case "${1:-}" in
  pull)  [ $# -eq 3 ] || die "dùng: pull <tag-đầy-đủ> <user@box>";  cmd_pull "$2" "$3" ;;
  push)  [ $# -eq 3 ] || die "dùng: push <tag-hoặc-alias> <user@box>"; cmd_push "$2" "$3" ;;
  list)  cmd_list ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
