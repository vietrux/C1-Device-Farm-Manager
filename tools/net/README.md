# per-container egress proxy

Set a SOCKS5/HTTP upstream per Android container from farm-console
(menu mỗi máy → **Proxy egress…**). The dobox image applies it to the whole
device (persist.s5 + tun2socks) via its `/proxy` extservice; we only drive it
through adbd — the same auditable push-and-run path as integrity-fix.

```
tools/net/device/proxy.sh   status | set <type> <ip> <port> [user] [pass] | clear
```
`type`: 2=socks5, 1=http. Prints one JSON line + `PROXY_OK`/`PROXY_FAIL`.

Console: `console/proxy.py` (`Proxy.status/set/clear`), endpoints
`GET .../proxy`, `POST .../proxy/set`, `POST .../proxy/clear`.

## Vì sao cần
Qua Play Integrity là **cần, chưa đủ**. X / ngân hàng / TikTok còn chấm điểm IP
và "quá nhiều thiết bị trên một IP". Farm nhiều máy chung một IP egress sẽ bị
soft-block (anti-ATO). Mỗi container một IP sạch (residential/mobile) là lớp
phòng thủ tách biệt với integrity.

## Giới hạn
- Chỉ kiểm cấu trúc call trên image `dobox:Q12_gms` (extservice `/proxy` :9082).
  Phản hồi vendor chưa chuẩn hoá — `status`/`set` trả `raw` để soi khi cần.
- Proxy nguồn (IP sạch) là của bạn; module này chỉ *đặt* vào máy, không cấp proxy.
