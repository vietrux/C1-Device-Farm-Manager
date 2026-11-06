# C1 Device Farm Manager

Web console điều khiển **device farm Android-in-Docker** chạy trên box RK3588 —
tạo/xoá máy, xem màn hình & chạm trực tiếp, quản lý Play Integrity (keybox) cho
từng máy. Truy cập box từ xa qua **Tailscale**, không cần mở cổng gì ra internet.

```
trình duyệt ──HTTP (localhost)──> farm-console (FastAPI, chạy trên MÁY CỦA BẠN)
                                      │  SSH (qua Tailscale) + adb tunnel
                                      ▼
                          box RK3588 ── docker bridge df-br
                              └── máy Android 1..N (container, GPU Mali thật)
```

> Dành cho việc quản lý thiết bị do bạn sở hữu/quản lý (kiểm thử, tự động hóa app).

## Yêu cầu

- Máy chạy console: Linux (đã kiểm trên Ubuntu), Python 3.10+, Node.js 18+ (build
  giao diện, chỉ lần đầu), `ssh`, `adb` (`sudo apt install adb` — hoặc scrcpy để
  có thêm nút mở scrcpy).
- **Truy cập box**: tham gia Tailscale **và** được chủ box share thiết bị box.
  Chủ box cung cấp: địa chỉ Tailscale (dạng `100.x.y.z`) + tài khoản SSH + mật
  khẩu sudo của box.
- Box: đã được chủ box provision sẵn (docker, `df-br`, tee-supplicant, Tailscale).
  Box **mới tinh chưa provision**: thêm vào `boxes.yaml` rồi bấm nút
  **Chuẩn hoá** — 1 bấm trọn gói: lược hệ vendor, cài resetprop (hồ sơ thiết
  bị cho máy), nạp image chính lên box nếu còn thiếu (cần kho image
  `~/.cache/devicefarm/images` — xem `tools/box/farm-image.sh` — và
  `./tools/integrity/fetch-assets.sh` cho resetprop).

## Cài đặt & chạy (một lệnh)

```bash
git clone https://github.com/vietrux/C1-Device-Farm-Manager.git
cd C1-Device-Farm-Manager
./tools/farm-console.sh
```

Lần đầu tự tạo môi trường Python (`.venv`) và build giao diện web. Sau đó mỗi
lần chạy: tự mở trình duyệt, **cổng ngẫu nhiên + token ngẫu nhiên, chỉ nghe
127.0.0.1** — không ai trên mạng khác với bạn chạm vào được. Đóng tab là console
tự tắt. Tắt hẳn: `Ctrl+C`. Build lại giao diện sau khi sửa code web:
`./tools/farm-console.sh --rebuild`.

## Thêm box

Cách nhanh: mở console → **+ Thêm box** → nhập SSH (`user@100.x.y.z`), mật khẩu
sudo → **Cài SSH key** (nhập mật khẩu SSH một lần, sau đó không cần nữa).

Hoặc tạo sẵn file `~/.config/farm-console/boxes.yaml` (xem `boxes.example.yaml`):

```yaml
boxes:
  - name: box-01
    ssh: user@100.x.y.z          # IP Tailscale của box
    sudo_password: mat-khau-sudo # của user trên box
    note: RK3588 ở nhà
```

## Tính năng

- **Máy Android**: tạo máy mới (chọn image/hồ sơ/kích thước), start/stop, xoá
  (giữ hoặc xoá data), log, tự chạy lại khi box khởi động lại.
- **Màn hình**: xem + chạm/vuốt/gõ chữ trực tiếp trên web; stream H.264 mượt;
  nút Back/Home/Nguồn; có scrcpy nếu laptop cài sẵn.
- **Hồ sơ thiết bị**: mỗi máy một hồ sơ (model, fingerprint, IMEI Luhn hợp lệ)
  áp từ lúc boot — nhất quán giữa các lần khởi động.
- **Play Integrity** (menu máy → *Integrity*): đo **trạng thái thật** — chuỗi
  keybox đang serve (đối chiếu md5 với sổ chain), props thiết bị, app đo lường;
  **Xoay keybox** = nhúng keybox mới vào image rồi tạo lại máy (~1 phút, không
  cần nướng lại image). Xem `tools/integrity/README.md`.
- **Proxy egress**: đặt/xoá proxy cho từng máy (socks5/http).
- **Nhiều box**: thêm bao nhiêu box tùy thích, mỗi box một dải port tunnel riêng.

## Keybox pool (riêng, KHÔNG kèm repo)

Keybox là dữ liệu nhạy cảm — repo này **không chứa keybox**. Chủ box chia sẻ
riêng file `*.xml` nếu muốn; bỏ vào `~/.config/farm-console/keyboxes/` là console
tự quét, kiểm định (chain, root, hạn, **danh sách revoke online của Google**)
và chấm điểm để chọn cái tốt nhất. Không có keybox vẫn điều khiển và xem trạng
thái bình thường.

## Bảo mật — đọc trước khi dùng

- Console **nghe 127.0.0.1 + token ngẫu nhiên** mặc định. Đừng mở ra mạng
  (`--host 0.0.0.0`) nếu không bắt buộc.
- Console chạy lệnh trên box **với sudo** (docker, network, container): ai chạy
  console + có SSH tới box = toàn quyền root trên box. Chỉ chạy trên máy bạn
  tin, không share phiên token cho ai.
- Tailscale: chủ box chỉ nên share **thiết bị box**, không share cả tailnet.
- Sau khi xong việc: tắt console (đóng tab), các tunnel SSH tự dọn.

## Cấu trúc

```
farm-console/   web console (FastAPI + Next.js static export)
  console/      API, điều khiển box qua SSH + adb, integrity, keybox pool
  web/          giao diện (Next.js) — build ra web/out
tools/
  farm-console.sh   launcher: tự cài venv + build + chạy
  box/df_agent.py   agent chạy TRÊN BOX qua SSH (list/create/rm/integrity…)
  box/farm-image.sh kho image trên laptop: pull về + 1 lệnh push sang box mới
                    (tự verify sha256 + md5 chuỗi keybox sau khi load)
  box/df-host.sh    cài đặt service trên box (chạy bởi console khi cần)
  integrity/        keybox embed + script đo trên máy Android
  net/              proxy egress cho máy Android
```
