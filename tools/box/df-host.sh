#!/bin/sh
# df-host.sh — chuẩn hoá OS host của box RK3588 (MYTOS / "syf3588-alpine") cho devicefarm.
#
# Chạy TRÊN box, quyền root. Không đụng kernel, bootloader, image hay data Android.
# Từ laptop thì dùng tools/provision-box.sh (đẩy script qua stdin, không để lại file).
#
#   df-host.sh check                 # chỉ đọc: báo cáo từng mục OK / CẦN SỬA
#   df-host.sh apply                 # áp dụng (idempotent, chạy lại bao lần cũng được)
#   df-host.sh apply --takeover      # như trên, cho phép tắt hệ vendor (mytsvr) nếu đang chạy
#   df-host.sh revert                # trả về cấu hình gốc đã backup lúc apply lần đầu
#
# Các mục:
#   vendor    mytsvr (agent thương mại của vendor) tắt + bỏ khỏi boot
#   tee       service df-tee giám sát tee-supplicant (image dobox block ở /dev/tee0 nếu thiếu)
#   docker    đóng Docker API TCP :2375 (không TLS/auth = root từ xa)
#   lockup    hard/soft lockup -> panic -> tự reboot (mặc định box treo vĩnh viễn)
#   sshd      AllowTcpForwarding local (cho tunnel adb của tools/android.sh)
#   network   docker bridge df-br
#   tune      service df-tune đặt governor CPU/GPU theo /etc/conf.d/df-tune
#   gpu       df-tune nạp firmware Mali từ host lúc boot, trước khi container nạp SELinux
#
# Biến môi trường (tuỳ chọn):
#   DF_DOCKER_TCP=127.0.0.1:2375   giữ Docker API nhưng chỉ nghe local (mặc định: tắt hẳn)
#   DF_BRIDGE=df-br  DF_SUBNET=172.30.0.0/24
#   DF_PANIC_SECS=10               số giây chờ trước khi reboot sau panic
#   DF_GPU_GOV / DF_CPU_GOV        governor ghi vào /etc/conf.d/df-tune khi tạo lần đầu
#                                  (mặc định performance = giống vendor, đã chạy ổn)
set -eu

STATE=/etc/devicefarm
BK=$STATE/backup
DOCKER_CONF=/etc/conf.d/docker
SSHD_CONF=/etc/ssh/sshd_config
SYSCTL_FILE=/etc/sysctl.d/90-devicefarm.conf
GPU=/sys/class/devfreq/fb000000.gpu
TEE_BIN=/usr/bin/tee-supplicant

DF_DOCKER_TCP="${DF_DOCKER_TCP:-}"
DF_BRIDGE="${DF_BRIDGE:-df-br}"
DF_SUBNET="${DF_SUBNET:-172.30.0.0/24}"
DF_PANIC_SECS="${DF_PANIC_SECS:-10}"
DF_GPU_GOV="${DF_GPU_GOV:-performance}"
DF_CPU_GOV="${DF_CPU_GOV:-performance}"

FAILS=0
ok()   { printf '  [ OK ]  %-8s %s\n' "$1" "$2"; }
bad()  { printf '  [SỬA]   %-8s %s\n' "$1" "$2"; FAILS=$((FAILS + 1)); }
skip() { printf '  [ -- ]  %-8s %s\n' "$1" "$2"; }
say()  { printf '  ...     %-8s %s\n' "$1" "$2"; }
die()  { echo "LỖI: $*" >&2; exit 2; }

enabled() { rc-update show default 2>/dev/null | grep -qE "^ *$1 \|"; }
running() { rc-service "$1" status >/dev/null 2>&1; }
want_docker_opts() {
  if [ -n "$DF_DOCKER_TCP" ]; then echo "-H unix:///var/run/docker.sock -H tcp://$DF_DOCKER_TCP"
  else echo "-H unix:///var/run/docker.sock"; fi
}
wait_docker() {   # dockerd cần vài giây sau restart mới nhận lệnh
  i=0
  until docker info >/dev/null 2>&1; do
    i=$((i + 1)); [ $i -le 60 ] || die "docker không lên lại sau 60s (xem /var/log/docker.log)"
    sleep 1
  done
}
cur_docker_opts() { sed -n 's/^DOCKER_OPTS="\(.*\)"$/\1/p' "$DOCKER_CONF" 2>/dev/null | tail -1; }

# lưu bản gốc đúng 1 lần (apply lần sau không ghi đè bản gốc thật)
backup_once() {
  mkdir -p "$BK"
  # (đặt trước .done để backup cũ của bản script trước cũng có mục này)
  [ -f "$BK/docker.enabled" ] || { enabled docker && echo yes || echo no; } > "$BK/docker.enabled"
  [ -f "$BK/.done" ] && return 0
  cp -p "$DOCKER_CONF" "$BK/conf.d-docker"
  cp -p "$SSHD_CONF" "$BK/sshd_config"
  { enabled mytsvr && echo yes || echo no; } > "$BK/mytsvr.enabled"
  for k in kernel.panic kernel.hardlockup_panic kernel.softlockup_panic; do
    echo "$k=$(sysctl -n $k)"
  done > "$BK/sysctl.orig"
  { cat $GPU/governor 2>/dev/null || true; } > "$BK/gpu.governor"
  for p in /sys/devices/system/cpu/cpufreq/policy*; do
    echo "$(basename $p)=$(cat $p/scaling_governor)"
  done > "$BK/cpu.governor"
  date > "$BK/.done"
  say backup "bản gốc lưu ở $BK"
}

preflight() {
  [ "$(id -u)" = 0 ] || die "cần chạy bằng root (sudo)"
  [ "$(uname -m)" = aarch64 ] || die "không phải aarch64"
  tr '\0' ' ' < /proc/device-tree/compatible 2>/dev/null | grep -q 'rockchip,rk3588' \
    || die "không phải RK3588"
  command -v rc-service >/dev/null || die "không có OpenRC"
  command -v docker >/dev/null || die "không có docker"
  slot=$(rauc status 2>/dev/null | sed -n 's/^Booted from: *//p')
  echo "Box: $(hostname)  kernel $(uname -r) $(uname -v | cut -d' ' -f1)  rauc: ${slot:-?}"
}

# =============================== CHECK ==================================
# container của vendor MYTOS: <md5>_<n>_T<id> (máy Android) và myt_sdk
vendor_containers() {
  running docker || return 0
  docker ps -a --format '{{.Names}}' | grep -E '^([0-9a-f]{32}_[0-9]+_T[0-9]+|myt_sdk)$' || true
}
check_vendor() {
  vc=$(vendor_containers | wc -l)
  if pidof mytsvr >/dev/null || enabled mytsvr; then
    bad vendor "mytsvr đang $(pidof mytsvr >/dev/null && echo chạy || echo tắt), boot=$(enabled mytsvr && echo bật || echo tắt), $vc container của hãng — apply cần --takeover"
  elif [ "$vc" != 0 ]; then
    bad vendor "mytsvr đã tắt nhưng còn $vc container của hãng — apply --takeover để xoá (image + data giữ lại)"
  else ok vendor "mytsvr tắt, không tự chạy khi boot, không còn container của hãng"; fi
}
check_tee() {
  if [ ! -c /dev/tee0 ] || [ ! -x $TEE_BIN ]; then skip tee "box không có /dev/tee0 hoặc $TEE_BIN"; return; fi
  if enabled df-tee && running df-tee && pidof tee-supplicant >/dev/null; then
    ok tee "df-tee chạy (pid $(pidof tee-supplicant)), bật khi boot"
  else bad tee "chưa có service giám sát tee-supplicant"; fi
}
check_docker() {
  # box vendor để mytsvr khởi động docker; tắt mytsvr thì docker phải tự bật khi boot
  if ! enabled docker || ! running docker; then
    bad docker "dockerd boot=$(enabled docker && echo bật || echo tắt), đang $(running docker && echo chạy || echo dừng)"
    return
  fi
  cur=$(cur_docker_opts)
  live=$(netstat -tln 2>/dev/null | grep -c ':2375 ' || true)
  if [ "$cur" = "$(want_docker_opts)" ]; then
    if [ -z "$DF_DOCKER_TCP" ] && [ "$live" != 0 ]; then bad docker "config đúng nhưng :2375 vẫn nghe (chưa restart docker)"
    else ok docker "DOCKER_OPTS=\"$cur\""; fi
  else bad docker "DOCKER_OPTS=\"$cur\" (API TCP mở không auth)"; fi
}
check_lockup() {
  h=$(sysctl -n kernel.hardlockup_panic); s=$(sysctl -n kernel.softlockup_panic); p=$(sysctl -n kernel.panic)
  if [ "$h" = 1 ] && [ "$s" = 1 ] && [ "$p" = "$DF_PANIC_SECS" ] && [ -f $SYSCTL_FILE ]; then
    ok lockup "treo kernel -> reboot sau ${p}s"
  else bad lockup "hardlockup_panic=$h softlockup_panic=$s panic=$p (treo = chết hẳn, phải rút điện)"; fi
}
check_sshd() {
  v=$(sed -n 's/^AllowTcpForwarding[[:space:]]\{1,\}//p' $SSHD_CONF | head -1)
  case "$v" in
    local|yes|all) ok sshd "AllowTcpForwarding $v" ;;
    *) bad sshd "AllowTcpForwarding ${v:-<mặc định>} (tunnel adb bị chặn)" ;;
  esac
}
check_network() {
  running docker || { bad network "không kiểm được: dockerd đang dừng"; return; }
  if docker network inspect "$DF_BRIDGE" >/dev/null 2>&1; then
    ok network "$DF_BRIDGE $(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "$DF_BRIDGE")"
  else bad network "chưa có docker network $DF_BRIDGE"; fi
}
check_tune() {
  if ! enabled df-tune; then bad tune "chưa có df-tune (governor đang do script vendor 'issue' ghim)"; return; fi
  . /etc/conf.d/df-tune
  g=$(cat $GPU/governor 2>/dev/null || echo ?)
  c=$(cat /sys/devices/system/cpu/cpufreq/policy*/scaling_governor | sort -u | tr '\n' ' ')
  if [ -n "${GPU_GOVERNOR:-}" ] && [ "$g" != "$GPU_GOVERNOR" ]; then bad tune "GPU=$g, conf muốn $GPU_GOVERNOR"
  else ok tune "GPU=$g @ $(($(cat $GPU/cur_freq) / 1000000))MHz, CPU=$c"; fi
}
check_gpu() {
  [ -c /dev/mali0 ] || { skip gpu "không có /dev/mali0"; return; }
  # dòng cuối cùng liên quan tới firmware trong dmesg của lần boot này
  last=$(dmesg | grep -E 'Loading Mali firmware|Firmware initialization failed' | tail -1)
  case "$last" in
    *"Loading Mali firmware"*) ok gpu "firmware Mali đã nạp (từ host, trước Android)" ;;
    *failed*) bad gpu "firmware Mali nạp LỖI ở boot này — đừng chạy container GPU, reboot sau khi apply" ;;
    *) if dmesg | grep -q 'Booting Linux'; then bad gpu "firmware Mali chưa nạp (container đầu tiên sẽ nạp dưới SELinux)"
       else skip gpu "không xác định (dmesg đã bị ghi đè)"; fi ;;
  esac
}
check_mali() {
  # container dùng --device /dev/mali0 mà KHÔNG bind node của host (-v /dev/mali0:/dev/mali0)
  # thì có inode riêng; dừng nó rồi start container khác làm kernel chết (HUONG-DAN 4.11)
  running docker || { skip mali "dockerd đang dừng"; return; }
  [ -c /dev/mali0 ] || { skip mali "không có /dev/mali0"; return; }
  badc=""; n=0
  for c in $(docker ps -aq); do
    # privileged: container tự có mọi node /dev (inode riêng) -> cùng rủi ro
    docker inspect -f '{{.HostConfig.Privileged}} {{range .HostConfig.Devices}}{{.PathOnHost}} {{end}}' "$c" \
      | grep -qE '^true|/dev/mali0' || continue
    n=$((n + 1))
    docker inspect -f '{{range .HostConfig.Binds}}{{.}} {{end}}' "$c" | grep -q '/dev/mali0:/dev/mali0' \
      || badc="$badc $(docker inspect -f '{{.Name}}' "$c" | tr -d /)"
  done
  if [ -n "$badc" ]; then bad mali "container thiếu -v /dev/mali0:/dev/mali0 (dừng/start sẽ treo box, tạo lại):$badc"
  else ok mali "$n container dùng GPU, tất cả bind /dev/mali0 của host"; fi
}
do_check() {
  echo "Kiểm tra:"
  check_vendor; check_tee; check_docker; check_lockup; check_sshd; check_network; check_tune; check_gpu; check_mali
  echo
  if [ $FAILS = 0 ]; then echo "=> Box đã chuẩn."; else echo "=> $FAILS mục cần sửa (chạy: apply)."; fi
  [ $FAILS = 0 ]
}

# =============================== APPLY ==================================
apply_vendor() {
  if ! pidof mytsvr >/dev/null && ! enabled mytsvr; then return; fi
  [ "$TAKEOVER" = 1 ] || die "hệ vendor (mytsvr) đang hoạt động trên box này. Tắt nó sẽ làm SDK/app quản lý của vendor ngừng hoạt động; container Android của vendor vẫn còn nhưng không ai quản. Chạy lại với --takeover nếu chắc chắn."
  rc-service mytsvr stop >/dev/null 2>&1 || true
  killall mytsvr 2>/dev/null || true
  rc-update del mytsvr default >/dev/null 2>&1 || true
  say vendor "đã tắt mytsvr + bỏ khỏi boot (file /etc/init.d/mytsvr giữ nguyên)"
}
apply_vendor_containers() {
  # chạy sau apply_docker (cần dockerd). Chỉ xoá container; image và data (/mmc/data) giữ nguyên.
  vc=$(vendor_containers)
  [ -n "$vc" ] || return 0
  [ "$TAKEOVER" = 1 ] || { say vendor "còn container của hãng ($(echo $vc)); chạy apply --takeover để xoá"; return 0; }
  for c in $vc; do
    docker update --restart no "$c" >/dev/null 2>&1 || true
    docker stop -t 20 "$c" >/dev/null 2>&1 || true
    docker rm "$c" >/dev/null && say vendor "đã xoá container $c (data + image giữ lại)"
  done
}
apply_tee() {
  [ -c /dev/tee0 ] && [ -x $TEE_BIN ] || return 0
  cat > /etc/init.d/df-tee <<'EOF'
#!/sbin/openrc-run
# devicefarm: OP-TEE supplicant cho /dev/tee0. Image dobox block lúc boot nếu thiếu.
description="OP-TEE tee-supplicant (devicefarm)"
supervisor=supervise-daemon
command=/usr/bin/tee-supplicant
supervise_daemon_args="--env LD_LIBRARY_PATH=/lib"
respawn_delay=2
respawn_max=0
depend() {
	need localmount
	before docker
}
EOF
  chmod 755 /etc/init.d/df-tee
  if ! running df-tee && pidof tee-supplicant >/dev/null; then
    killall tee-supplicant; sleep 1   # bản chạy tay / của mytsvr: 2 supplicant trên tee0 là lỗi
  fi
  enabled df-tee || rc-update add df-tee default >/dev/null
  running df-tee || rc-service df-tee start >/dev/null
  sleep 1
  pidof tee-supplicant >/dev/null || die "df-tee không giữ được tee-supplicant (xem /var/log/messages)"
  say tee "df-tee chạy, bật khi boot"
}
apply_docker() {
  if ! enabled docker; then
    rc-update add docker default >/dev/null
    say docker "bật dockerd khi boot"
  fi
  if ! running docker; then
    rc-service docker start >/dev/null
    wait_docker
    say docker "đã start dockerd"
  fi
  want=$(want_docker_opts)
  if [ "$(cur_docker_opts)" != "$want" ]; then
    if grep -q '^DOCKER_OPTS=' $DOCKER_CONF; then
      sed -i "s|^DOCKER_OPTS=.*|DOCKER_OPTS=\"$want\"|" $DOCKER_CONF
    else echo "DOCKER_OPTS=\"$want\"" >> $DOCKER_CONF; fi
    grep -q '"live-restore": *true' /etc/docker/daemon.json 2>/dev/null \
      || say docker "CẢNH BÁO: không có live-restore, restart docker sẽ dừng container"
    rc-service docker restart >/dev/null
    wait_docker
    say docker "DOCKER_OPTS=\"$want\", đã restart docker"
  fi
  [ -n "$DF_DOCKER_TCP" ] || ! netstat -tln 2>/dev/null | grep -q ':2375 ' \
    || die ":2375 vẫn mở sau khi restart docker"
}
apply_lockup() {
  cat > $SYSCTL_FILE <<EOF
# devicefarm: kernel treo (Mali GPU wedge -> hard LOCKUP) thì panic rồi tự reboot,
# thay vì treo vĩnh viễn chờ rút điện. Box không có /dev/watchdog phần cứng.
kernel.hardlockup_panic = 1
kernel.softlockup_panic = 1
kernel.panic = $DF_PANIC_SECS
EOF
  sysctl -q -p $SYSCTL_FILE
  say lockup "treo kernel -> reboot sau ${DF_PANIC_SECS}s"
}
apply_sshd() {
  v=$(sed -n 's/^AllowTcpForwarding[[:space:]]\{1,\}//p' $SSHD_CONF | head -1)
  case "$v" in local|yes|all) return ;; esac
  cp -p $SSHD_CONF $SSHD_CONF.df-tmp
  if grep -q '^AllowTcpForwarding' $SSHD_CONF; then
    sed -i 's/^AllowTcpForwarding.*/AllowTcpForwarding local/' $SSHD_CONF
  elif grep -q '^Match' $SSHD_CONF; then
    sed -i '0,/^Match/s//AllowTcpForwarding local\n&/' $SSHD_CONF   # phải nằm trước block Match
  else echo 'AllowTcpForwarding local' >> $SSHD_CONF; fi
  if /usr/sbin/sshd -t; then
    rm -f $SSHD_CONF.df-tmp
    rc-service sshd reload >/dev/null   # reload: phiên SSH đang mở không bị cắt
    say sshd "AllowTcpForwarding local, đã reload sshd"
  else
    mv $SSHD_CONF.df-tmp $SSHD_CONF
    die "sshd -t báo lỗi, đã trả lại sshd_config"
  fi
}
apply_network() {
  docker network inspect "$DF_BRIDGE" >/dev/null 2>&1 && return
  docker network create --driver bridge --subnet "$DF_SUBNET" "$DF_BRIDGE" >/dev/null
  say network "tạo $DF_BRIDGE $DF_SUBNET"
}
apply_tune() {
  if [ ! -f /etc/conf.d/df-tune ]; then
    cat > /etc/conf.d/df-tune <<EOF
# devicefarm: governor áp dụng lúc boot (sau script vendor 'issue').
# GPU: performance | simple_ondemand | powersave | userspace     (để trống = không đụng)
# CPU: performance | schedutil | ondemand | conservative | powersave
GPU_GOVERNOR="$DF_GPU_GOV"
GPU_MAX_FREQ=""        # Hz, vd 800000000 để hạ trần GPU; trống = mặc định 1 GHz
CPU_GOVERNOR="$DF_CPU_GOV"
EOF
  fi
  cat > /etc/init.d/df-tune <<'EOF'
#!/sbin/openrc-run
# devicefarm: governor CPU/GPU theo /etc/conf.d/df-tune
description="CPU/GPU governor (devicefarm)"
depend() {
	after issue
	before docker
}
start() {
	# Nạp firmware GPU ngay từ host, TRƯỚC docker. Mali chỉ nạp firmware ở lần mở
	# /dev/mali0 đầu tiên; nếu đó là tiến trình Android sau khi container đã nạp
	# SELinux enforcing vào kernel chung thì bị chặn (-13) -> GPU hỏng -> box treo.
	[ -c /dev/mali0 ] && : < /dev/mali0
	ebegin "Applying df-tune governors"
	g=/sys/class/devfreq/fb000000.gpu
	[ -n "${GPU_GOVERNOR:-}" ] && [ -d $g ] && echo "$GPU_GOVERNOR" > $g/governor
	[ -n "${GPU_MAX_FREQ:-}" ] && [ -d $g ] && echo "$GPU_MAX_FREQ" > $g/max_freq
	if [ -n "${CPU_GOVERNOR:-}" ]; then
		for p in /sys/devices/system/cpu/cpufreq/policy*; do
			echo "$CPU_GOVERNOR" > $p/scaling_governor
		done
	fi
	eend 0
}
EOF
  chmod 755 /etc/init.d/df-tune
  enabled df-tune || rc-update add df-tune default >/dev/null
  rc-service df-tune restart >/dev/null
  say tune "df-tune theo /etc/conf.d/df-tune"
}
do_apply() {
  echo "Áp dụng:"
  backup_once
  apply_vendor; apply_tee; apply_docker; apply_vendor_containers; apply_lockup; apply_sshd; apply_network; apply_tune
  echo; do_check
}

# =============================== REVERT =================================
do_revert() {
  [ -f "$BK/.done" ] || die "không có backup ở $BK (box chưa từng apply)"
  echo "Trả về cấu hình gốc:"
  for s in df-tune df-tee; do
    running $s && rc-service $s stop >/dev/null 2>&1 || true
    rc-update del $s default >/dev/null 2>&1 || true
    rm -f /etc/init.d/$s
  done
  rm -f /etc/conf.d/df-tune
  say tune "gỡ df-tee, df-tune"
  g=$(cat $BK/gpu.governor); [ -n "$g" ] && echo "$g" > $GPU/governor
  while IFS== read -r p v; do echo "$v" > /sys/devices/system/cpu/cpufreq/$p/scaling_governor; done < $BK/cpu.governor
  rm -f $SYSCTL_FILE
  while IFS== read -r k v; do sysctl -q -w "$k=$v"; done < $BK/sysctl.orig
  say lockup "sysctl về như cũ"
  if ! cmp -s $BK/sshd_config $SSHD_CONF; then
    cp -p $BK/sshd_config $SSHD_CONF && rc-service sshd reload >/dev/null
    say sshd "sshd_config gốc"
  fi
  if ! cmp -s $BK/conf.d-docker $DOCKER_CONF; then
    cp -p $BK/conf.d-docker $DOCKER_CONF && rc-service docker restart >/dev/null
    wait_docker
    say docker "conf.d/docker gốc, đã restart docker"
  fi
  if [ "$(cat $BK/docker.enabled 2>/dev/null)" = no ]; then
    rc-update del docker default >/dev/null 2>&1 || true
    say docker "dockerd không tự bật khi boot (như gốc)"
  fi
  if [ "$(cat $BK/mytsvr.enabled)" = yes ]; then
    rc-update add mytsvr default >/dev/null; rc-service mytsvr start >/dev/null || true
    say vendor "bật lại mytsvr"
  fi
  skip network "giữ $DF_BRIDGE (xoá tay: docker network rm $DF_BRIDGE)"
  rm -rf "$BK"
  echo "Xong. Backup đã dùng và xoá."
}

main() {
  cmd="${1:-check}"; TAKEOVER=0
  [ "${2:-}" = --takeover ] && TAKEOVER=1
  preflight
  case "$cmd" in
    check)  do_check ;;
    apply)  do_apply ;;
    revert) do_revert ;;
    *) die "lệnh không hợp lệ: $cmd (check | apply [--takeover] | revert)" ;;
  esac
}
# toàn bộ script được đọc xong trước khi chạy (an toàn khi đẩy qua stdin: sh -s)
main "$@" </dev/null
