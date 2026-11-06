# integrity-fix

Xoay chuỗi keybox **Play Integrity (DEVICE)** cho từng máy Android trong farm,
điều khiển từ farm-console (menu mỗi máy → **Integrity** → **Xoay keybox**).

## Cơ chế — theo cái ĐÃ CHỨNG MINH (2026-11-06, debug tận gốc trên máy-thật)

| Nửa | Nguồn thật | Ghi chú |
|---|---|---|
| **DEVICE** | chuỗi keybox **nhúng** trong `/vendor/lib64/libsoft_attestation_cert.so` của image (vùng `@0x82254000` trong super.img) | HAL keymaster **không bao giờ đọc** `/data/system/keymaster.xml` (atime không đổi qua nhiều attest tươi). Xoay = thay data lib trong layer image rồi **tạo lại máy** — xem `tools/box/GOLDEN-BAKE.md` |
| **BASIC** | props thiết bị: MYTPIF dựng sẵn trong framework + hồ sơ `resetprop` (fingerprint Pixel, `flash.locked=1`, vbmeta locked) | Magisk/Zygisk/PIF **không tải được** trong container — không dùng nữa |

Đổi keybox **không cần nướng lại image**: laptop chạy `tools/box/keybox_embed.py`
trên lib gốc (data-only swap) → agent ghi vào layer → console tạo lại máy
(~60s; `docker restart` làm mất netns wiring nên phải delete+create).

## Thành phần

```
tools/integrity/
  fetch-assets.sh        tải Key Attestation + SPIC (pinned + sha256) vào cache máy console
  device/
    remove.sh            DỌN DI SẢN legacy: gỡ magisk+module, xoá keymaster.xml (không đổi chain nhúng)
    status.sh            đo SỰ THẬT: md5 lib chain, props thật, app đo lường (JSON, không tự đoán "fixed")
console/
  integrity.py           status: đối chiếu md5 với sổ chain (~/.cache/devicefarm/integrity/chains.json)
                         → verdict ok/revoked/expired/factory/unknown;
                         rotate: keybox_embed → ghi layer → (app.py) tạo lại máy
  keybox.py              nạp + validate pool keybox (root, hạn, revoke của Google)
tools/box/
  keybox_embed.py        nhúng keybox vào lib (data-only, giữ nguyên code patch)
  df_agent.py            lệnh box: dev-info / image-lib / patch-image-lib
```

**Sổ chain** (`chains.json`): md5 lib → tên keybox + revoked/hạn. Ghi mỗi lần
xoay (trạng thái theo **nửa EC** — nửa được serve cho Google attestation; RSA
bị revoke nhưng EC sạch vẫn hiện "chain sạch"). Seed tay cho chain factory/trickbox.

## Dùng

- **Web**: menu máy → **Integrity** — thấy ngay chain thật đang serve, props,
  app đo lường, verdict. Chọn keybox → **Xoay keybox**. **Dọn di sản** chỉ xuất
  hiện khi còn artefact magisk/PIF/keymaster.xml.
- **API**:
  - `GET  /api/keyboxes` — pool + validate
  - `GET  /api/boxes/{box}/devices/{dev}/integrity` — trạng thái thật
  - `POST /api/boxes/{box}/devices/{dev}/integrity/apply` `{"keybox": "<tên>|null"}` — xoay
  - `POST /api/boxes/{box}/devices/{dev}/integrity/remove` — dọn di sản

## Cách chấm điểm keybox

`usable` = có ≥1 key root `google`/`droid_rkp`, chain + chữ ký hợp lệ, còn hạn,
**không** nằm trong danh sách revoke của Google (online, fallback `roots/status.json`).
Điểm cao hơn khi: root `google` (hardware cổ điển) > `droid_rkp` (RKP ngắn hạn),
đủ 2 thuật toán (ecdsa+rsa), hạn còn dài. `best()` chọn điểm cao nhất.
Verdict của máy lấy trạng thái **nửa EC** của keybox đang serve.

## Ràng buộc nhúng keybox (keybox_embed.py)

- đủ cặp ecdsa+rsa, mỗi chain đúng 3 cert, root của 2 chain **giống hệt byte**
  (slot shared-root); tổng ≤ (0x2E2EE − 0x2C3A8) — tràn thì chọn keybox nhỏ hơn;
- RSA key phải có header `RSA PRIVATE KEY` đúng nội dung (không PKCS#8 lẫn PKCS#1).

## Giới hạn

- Chỉ kiểm trên image `dobox:Q12_gms` (Android 12, keymaster-4-0). Image khác
  cần đo lại offset lib trong super (`0x82254000` là layout golden).
- Root của [keybox] hết hạn 2026-05-24 (leaf/int đến 2028-04-15) — PI sẽ trả lời
  root hết hạn có được chấm không; đo bằng app SPIC trước khi rollout fleet.
- Thước đo verdict thật cần app checker có cloud project number (classic request
  trả UNEVALUATED từ 2025).
