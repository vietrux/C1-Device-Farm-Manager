#!/system/bin/sh
# Per-container egress proxy, driven through the dobox image's own extservice
# (/proxy on :9082). SOCKS5/HTTP upstream is applied by the image to the whole
# container (persist.s5 + tun2socks), so every app in the device egresses through
# the chosen IP. No root logic of ours — we only call the vendor endpoint.
#
#   proxy.sh status
#   proxy.sh set <type> <ip> <port> [user] [pass]   # type: 2=socks5 1=http
#   proxy.sh clear                                   # back to direct egress
#
# Prints a single JSON line plus PROXY_OK / PROXY_FAIL.
set -u

PORT="${EXT_PORT:-9082}"
BASE="http://127.0.0.1:${PORT}/proxy"
CURL=/system/xbin/curl
[ -x "$CURL" ] || CURL=curl

say_json() { printf '{"applied":%s,"ip":"%s","port":"%s","type":"%s","raw":"%s"}\n' "$1" "$2" "$3" "$4" "$5"; }

esc() { printf '%s' "$1" | sed 's/[&=]/_/g; s/"/'"'"'/g'; }  # keep response one-line/safe

call() {
    # usage: call "query string"
    timeout 25 "$CURL" -sS -m 20 "${BASE}?$1" 2>&1
}

case "${1:-status}" in
  status)
    RESP=$(call "cmd=1")
    say_json "null" "" "" "" "$(esc "$RESP")"
    echo PROXY_OK
    ;;
  set)
    TYPE="${2:?type}"; IP="${3:?ip}"; PRT="${4:?port}"; USR="${5:-}"; PWD="${6:-}"
    Q="cmd=2&type=${TYPE}&ip=${IP}&port=${PRT}"
    [ -n "$USR" ] && Q="${Q}&usr=${USR}"
    [ -n "$PWD" ] && Q="${Q}&pwd=${PWD}"
    RESP=$(call "$Q")
    say_json "true" "$IP" "$PRT" "$TYPE" "$(esc "$RESP")"
    case "$RESP" in
      *fail*|*error*|*Error*|*失败*) echo PROXY_FAIL: "$RESP" ;;
      *) echo PROXY_OK ;;
    esac
    ;;
  clear)
    RESP=$(call "cmd=3")
    # cmd=4 writes direct.txt (hard direct) as a belt-and-braces fallback
    RESP2=$(call "cmd=4")
    say_json "false" "" "" "" "$(esc "$RESP $RESP2")"
    echo PROXY_OK
    ;;
  *)
    echo "PROXY_FAIL: unknown action '$1'" ; exit 2 ;;
esac
