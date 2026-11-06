# farm-console

Giao diện web quản lý nhiều box RK3588 và các máy Android chạy trên đó.

Quy trình với một box mới mua:

1. **Thêm box**: nhập địa chỉ SSH và mật khẩu sudo.
2. **Cài SSH key**: nhập mật khẩu SSH một lần.
3. **Lược bỏ hệ vendor**:
   - tắt agent của hãng;
   - xoá các container của hãng (image và data vẫn giữ trên đĩa);
   - đóng Docker API đang mở ra mạng;
   - cài các service của mình: tee-supplicant, tune GPU và nạp firmware lúc boot, tự reboot khi treo, mạng `df-br`.
4. **Tạo máy**: binder và IP được cấp tự động, luôn dùng công thức đã kiểm chứng (có bind `/dev/mali0`). Console chờ Android boot xong rồi mới bật tự chạy khi box khởi động lại.
5. **Màn hình**: xem và điều khiển máy ngay trên web, bấm là chạm, kéo là vuốt. Có các nút Back/Home/Nguồn và ô gõ chữ. Nếu console chạy trên laptop có scrcpy thì có thêm nút mở scrcpy.

## Kiến trúc

```
trình duyệt ──HTTP──> farm-console (FastAPI, chạy trên laptop/máy chủ trong Tailscale)
                          │  ~/.config/farm-console/boxes.yaml  (0600, có mật khẩu sudo)
                          │
                          ├─SSH (ControlMaster)─> box: sudo python3 - <lệnh>   ← tools/box/df_agent.py qua stdin
                          ├─SSH───────────────> box: sudo sh -s <mode>         ← tools/box/df-host.sh qua stdin
                          └─SSH port-forward──> <ip máy>:5555 (adbd)  ← màn hình + chạm qua adb
```

- **Box không cần cài thêm gì.** Mỗi lệnh đẩy script qua stdin, dùng Python 3 và docker có sẵn trên box. Kết nối SSH được dùng lại (ControlMaster), nên thao tác nhanh.
- **`df_agent.py`** là nơi duy nhất chứa công thức tạo container. Nó tự chọn bộ binder `3k+1..3k+3` còn trống và IP trống từ `.50`. Nó từ chối tạo hoặc start máy trong các trường hợp:
  - còn container dùng GPU mà thiếu bind `/dev/mali0` (dừng/start lẫn với chúng sẽ treo box, xem HUONG-DAN.md mục 4.11);
  - tee-supplicant không chạy.
- **Màn hình và chạm đi qua adbd**, không dùng `docker exec`. Tiến trình `docker exec` mang context SELinux `u:r:kernel:s0`, và policy Android chặn nó gọi binder tới SurfaceFlinger, nên lệnh `screencap` sẽ treo. Public key adb của máy chạy console (`~/.android/adbkey.pub`) được cài vào máy lúc tạo.
- **Lúc tạo máy, console theo dõi load.** Nếu load 1 phút đạt từ 12 trở lên trong lúc boot thì máy bị dừng ngay; đổi ngưỡng bằng `FARM_CONSOLE_LOAD_GUARD`.

## Bật / tắt

farm-console chạy như một app: bật khi cần, xong thì tắt.

```bash
./tools/farm-console.sh             # bật (lần đầu tự tạo .venv và build web/out)
./tools/farm-console.sh --install   # thêm biểu tượng vào menu ứng dụng (--uninstall để gỡ)
./tools/farm-console.sh --rebuild   # build lại giao diện rồi bật
./tools/farm-console.sh -- --help   # các cờ khác của farm-console
```

Mỗi lần bật, console:
- **chọn cổng ngẫu nhiên đang trống**: mở socket ở cổng 0 cho hệ điều hành cấp rồi giao thẳng cho uvicorn, nên không trùng service khác và không có khoảng hở để bị chiếm cổng;
- **sinh token ngẫu nhiên**, gắn sẵn vào link mở trình duyệt; API không có token trả 401;
- tự mở trình duyệt (`--no-browser` để tắt việc này).

Khi tắt:
- Trang web gửi ping mỗi 30 giây, kể cả khi tab ẩn. Không còn ping/request trong `--idle-exit` giây (mặc định 180) và không còn tác vụ đang chạy thì console tự thoát.
- Lúc thoát, console đóng các kết nối SSH master và port forward adb.
- Tab còn mở sẽ hiện “farm-console đã tắt”.

Cần có: Python ≥ 3.10, Node ≥ 20 (chỉ để build giao diện), `ssh`, `adb`; tuỳ chọn `sshpass` (cho nút cài key) và `scrcpy`.

| Cờ | Mặc định | Ý nghĩa |
|---|---|---|
| `--port` | `0` = ngẫu nhiên | Cổng cố định nếu cần |
| `--host` | `127.0.0.1` | Mở ra mạng (vd IP Tailscale) thì token bắt buộc |
| `--idle-exit` | `180` | Giây không dùng trước khi tự tắt; `0` = chạy mãi |
| `--token` / `--no-token` | ngẫu nhiên | Token cố định, hoặc tắt token (chỉ để debug) |
| `--no-browser` | — | Không tự mở trình duyệt |
| `--boxes` | `~/.config/farm-console/boxes.yaml` | Danh sách box (quyền 0600, có mật khẩu sudo) |
| `--scripts` | `<repo>/tools/box` | Thư mục chứa `df_agent.py`, `df-host.sh` |
| `--adbkey` | `~/.android/adbkey.pub` | Key adb được cài vào máy mới |

## Phát triển giao diện

`web/` là project Next.js khởi tạo bằng `npx shadcn@latest init --preset b44ZeGgJU --template next`, dùng component shadcn/ui (style base-lyra, Base UI). Được build tĩnh (`output: "export"`) và FastAPI phục vụ thư mục `web/out`.

```bash
cd web
npm run typecheck && npm run lint
npm run build            # sau đó tải lại trang console
npx shadcn@latest add <component>
```

## Test

```bash
.venv/bin/python -m pytest -q     # API (Remote/adb giả) + phần thuần của df_agent.py
```
