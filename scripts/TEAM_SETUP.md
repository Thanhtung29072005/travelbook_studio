# Chạy TravelBook theo nhóm, dùng chung 9Router

**Nếu các thành viên ở khác mạng/Wi-Fi:** dùng [hướng dẫn Radmin VPN](RADMIN_SETUP.md) cho phần kết nối máy chủ. Phần B bên dưới vẫn hướng dẫn cài TravelBook trên máy từng người.

Mô hình trong hướng dẫn này:

```text
Trình duyệt của thành viên
          |
          v
TravelBook trên máy thành viên (giao diện + backend)
          |
          v
9Router trên máy chủ của trưởng nhóm
          |
          v
Các model AI đã được cấu hình trong 9Router
```

Mỗi thành viên có dữ liệu dự án và PDF riêng trên máy mình. Chỉ dịch vụ AI là dùng chung.
Không cần cài Codex hoặc đăng nhập Codex để chạy TravelBook.

## A. Trưởng nhóm: chuẩn bị máy chủ 9Router

### 1. Kiểm tra 9Router đang chạy

Mở 9Router như bạn vẫn sử dụng. Kiểm tra tài khoản/provider còn hoạt động và có model dùng được.
TravelBook hiện cấu hình API mặc định tại `http://127.0.0.1:20128/v1`.
Đây là địa chỉ API, không nhất thiết là địa chỉ trang quản trị 9Router.
Nếu bản cài của bạn dùng cổng khác, thay `20128` trong các bước bên dưới.

Chuẩn bị và gửi riêng cho từng thành viên:

- Địa chỉ API được chia sẻ ở bước 4.
- API key do 9Router cấp. Không gửi mật khẩu tài khoản provider hay token đăng nhập provider.
- ID model văn bản, model hỗ trợ đọc ảnh và model sinh ảnh từ danh sách model thực tế của 9Router.

Không tự đặt tên model. Nếu 9Router hỗ trợ nhiều API key, nên cấp riêng cho từng thành viên để dễ thu hồi.

### 2. Tìm địa chỉ IP của máy bạn

Máy bạn và các thành viên cần ở cùng mạng LAN/Wi-Fi có thể kết nối với nhau.
Mở PowerShell và chạy:

```powershell
ipconfig
```

Tìm **IPv4 Address** của Wi-Fi/Ethernet đang dùng. Ví dụ: `192.168.1.10`.
Đây chỉ là ví dụ: mọi người phải dùng IP thực tế của máy bạn.
Nếu IP hay thay đổi, có thể đặt DHCP reservation cho máy này trên router.

### 3. Chia sẻ API ra mạng nội bộ

Trước tiên xem 9Router đang lắng nghe ở địa chỉ nào:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 20128 |
    Select-Object LocalAddress, LocalPort
```

**Nếu có `0.0.0.0` hoặc IP LAN của máy bạn:** 9Router đã nhận kết nối IPv4 từ mạng.
Mở **PowerShell bằng Run as administrator**, rồi cho phép cổng 20128 trong mạng Private:

```powershell
New-NetFirewallRule -DisplayName "TravelBook 9Router LAN" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 20128 -Profile Private -RemoteAddress LocalSubnet
```

Địa chỉ chia sẻ trong trường hợp này là `http://192.168.1.10:20128/v1`.

**Nếu chỉ có `127.0.0.1`:** 9Router chỉ nhận kết nối trên máy bạn. Nếu bản 9Router đang dùng có tùy chọn bind/listen host, có thể đặt thành `0.0.0.0`, khởi động lại và dùng cách trên. Tên tùy chọn tùy phiên bản; không phải mọi bản đều có cùng giao diện.

Một cách khác trên Windows, không cần thay cấu hình 9Router, là chuyển tiếp cổng **20129** tới API cục bộ **20128**. Chạy các lệnh sau trong **PowerShell bằng Run as administrator**:

```powershell
Start-Service iphlpsvc
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=20129 connectaddress=127.0.0.1 connectport=20128
New-NetFirewallRule -DisplayName "TravelBook 9Router LAN proxy" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 20129 -Profile Private -RemoteAddress LocalSubnet
netsh interface portproxy show all
```

Địa chỉ chia sẻ trong trường hợp chuyển tiếp là `http://192.168.1.10:20129/v1`.

Chọn **một** cách và thông báo đúng cổng cho cả nhóm. Cách chuyển tiếp IPv4 này cần API gốc truy cập được tại `127.0.0.1:20128`.
Các quy tắc trên chỉ áp dụng khi mạng Windows được đặt là **Private**. Chỉ đặt Private cho mạng bạn tin cậy.
Không cần mở cổng quản trị 9Router hoặc cổng TravelBook trên máy bạn cho mô hình này.

### 4. Kiểm tra từ máy một thành viên

Ví dụ bạn chọn cách chuyển tiếp cổng 20129, thành viên mở PowerShell và chạy:

```powershell
Test-NetConnection 192.168.1.10 -Port 20129
```

`TcpTestSucceeded : True` nghĩa là đã kết nối được cổng. Nếu dùng trực tiếp thì thay cổng bằng `20128`.

Kiểm tra thêm API và khóa truy cập. Nhập key khi được hỏi để tránh ghi key trực tiếp vào lịch sử lệnh:

```powershell
$teamRouterKey = Read-Host "API key 9Router" -AsSecureString
$teamRouterCredential = [System.Net.NetworkCredential]::new("", $teamRouterKey)
$teamRouterHeaders = @{ Authorization = "Bearer " + $teamRouterCredential.Password }
Invoke-RestMethod -Uri "http://192.168.1.10:20129/v1/models" -Headers $teamRouterHeaders
Remove-Variable teamRouterKey, teamRouterCredential, teamRouterHeaders
```

Danh sách trả về cho biết các ID model mà key đó nhìn thấy. Có model trong danh sách chưa bảo đảm tài khoản còn quota hoặc model có hỗ trợ vision/sinh ảnh; cần kiểm tra từng chức năng trong 9Router/TravelBook.
Nếu bản 9Router không hỗ trợ `/v1/models`, kiểm tra API/model bằng chức năng thử kết nối của bản đang cài.

### 5. Giữ máy chủ hoạt động

- Máy bạn phải bật, có Internet và 9Router phải đang chạy trong lúc nhóm sử dụng.
- Tạm tránh để máy tự Sleep khi cả nhóm đang chạy dự án.
- Các lượt gọi AI dùng tài khoản/quota đã cấu hình trên 9Router của bạn. Nên thử một dự án trước rồi mới tăng số người chạy đồng thời.
- Chỉ chia sẻ trong mạng tin cậy. Không mở cổng này trực tiếp ra Internet bằng port forwarding trên router.
- Nếu khác mạng, có thể dùng VPN riêng như Tailscale. Khi đó dùng IP VPN và cấu hình tường lửa cho đúng địa chỉ VPN được phép; quy tắc `LocalSubnet` ở trên không mặc nhiên áp dụng cho mọi kết nối VPN.

## B. Mỗi thành viên: cài TravelBook

### 1. Cài công cụ

Hướng dẫn này dành cho Windows PowerShell. Cài:

- Git: <https://git-scm.com/downloads>
- Python 3.12: <https://www.python.org/downloads/> — bật Python launcher khi cài.
- Node.js 24 LTS: <https://nodejs.org/>

Đóng và mở lại PowerShell sau khi cài. Kiểm tra:

```powershell
git --version
py -3.12 --version
node --version
npm.cmd --version
```

### 2. Tải mã nguồn

Chuyển đến thư mục bạn muốn lưu dự án, rồi chạy:

```powershell
git clone https://github.com/Thanhtung29072005/travelbook_studio.git
cd travelbook_studio
```

Nếu kho được đặt Private, trưởng nhóm cần cấp quyền GitHub cho từng thành viên trước.

### 3. Cài phần backend

Tại thư mục gốc `travelbook_studio`:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Các lệnh dùng trực tiếp Python trong `.venv`, không cần chạy Activate.ps1 hoặc thay Execution Policy.

### 4. Tạo cấu hình riêng

Nếu chưa có `.env`, tạo từ mẫu:

```powershell
Copy-Item .env.example .env
notepad .env
```

Không chạy lệnh copy lại nếu đã có `.env` chứa cấu hình đang dùng.
Trong `.env`, **sửa các dòng có sẵn**, không thêm nhiều dòng trùng tên:

```dotenv
# IP/cổng do trưởng nhóm cung cấp; ví dụ này dùng chuyển tiếp 20129.
NINE_ROUTER_BASE_URL=http://192.168.1.10:20129/v1
NINE_ROUTER_API_KEY=
NINE_ROUTER_MODELS=
NINE_ROUTER_VISION_MODELS=
NINE_ROUTER_IMAGE_MODEL=

APP_HOST=127.0.0.1
APP_PORT=8000
FRONTEND_ORIGIN=http://localhost:5173

PLANNER_DAY_START_HOUR=8
PLANNER_DAY_END_HOUR=21
PLANNER_MAX_STOPS=6
```

Điền key và các ID model trưởng nhóm cung cấp vào các giá trị đang để trống:

- `NINE_ROUTER_MODELS`: model viết/hiểu văn bản, trả được JSON; có thể ghi nhiều ID phân cách bằng dấu phẩy để dự phòng.
- `NINE_ROUTER_VISION_MODELS`: model đọc được ảnh, dùng cho Reviewer.
- `NINE_ROUTER_IMAGE_MODEL`: model có thể sinh ảnh qua API ảnh mà TravelBook sử dụng.

Một model văn bản không mặc nhiên có thể sinh ảnh. Chưa có model sinh ảnh phù hợp thì tính năng minh họa AI chưa thể hoạt động đầy đủ.
Giữ `/v1` ở cuối base URL, không thêm `/chat/completions`.
**Không dùng `localhost` cho địa chỉ 9Router trên máy thành viên**, vì `localhost` sẽ trỏ về chính máy thành viên.
Giữ `.env` ở máy, không đưa key vào `.env.example` hay commit lên GitHub.

### 5. Cài giao diện

Từ thư mục gốc:

```powershell
cd frontend
npm.cmd ci
cd ..
```

### 6. Chạy backend — cửa sổ PowerShell thứ nhất

Mở PowerShell tại thư mục gốc `travelbook_studio` và chạy:

```powershell
.\.venv\Scripts\python.exe -m backend.app.main
```

Để cửa sổ này mở. Backend mặc định ở `http://127.0.0.1:8000`.
Có thể mở `http://127.0.0.1:8000/docs` để kiểm tra API TravelBook đã chạy.

### 7. Chạy giao diện — cửa sổ PowerShell thứ hai

Mở một PowerShell khác tại thư mục gốc:

```powershell
cd frontend
npm.cmd run dev
```

Mở **<http://localhost:5173>** bằng trình duyệt. Giữ cả hai cửa sổ đang chạy.
Nếu Vite báo cổng 5173 đã dùng và chuyển sang cổng khác, dừng tiến trình cũ hoặc cập nhật cấu hình phù hợp; hướng dẫn này dùng cổng 5173.

### 8. Kiểm tra model và tạo dự án

1. Vào **Model 9Router**, kiểm tra kết nối và chọn đúng model đã cấu hình. Nếu từng lưu cấu hình qua giao diện, kiểm tra cả các giá trị đã lưu ở đó.
2. Chọn **Dự án mới**, nhập điểm đến, ngày đi cụ thể, số người/tuổi trẻ nhỏ, ngân sách cả nhóm, nơi khởi hành và giờ đi/về nếu đã có vé.
3. Nếu chưa chốt vé, ghi rõ đồng ý dùng lịch tạm. Nếu thiếu thông tin, trả lời phần **Bổ sung thông tin** để tiếp tục.
4. Nếu bật duyệt thủ công, xử lý yêu cầu duyệt khi dự án dừng chờ.
5. Mở **Sản phẩm** để xem trước hoặc tải PDF sau khi hoàn tất.

## C. Các lần sử dụng sau

Trưởng nhóm mở 9Router. Mỗi thành viên chỉ cần chạy lại hai cửa sổ ở B.6 và B.7.
Không cần cài lại thư viện mỗi lần. Sửa `.env` thì dừng backend bằng `Ctrl+C`, sau đó chạy lại backend.

Khi có cập nhật mã nguồn, dừng TravelBook trước rồi chạy tại thư mục gốc:

```powershell
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
cd frontend
npm.cmd ci
cd ..
```

Nếu Git báo thay đổi cục bộ, lưu/commit thay đổi trước; không xóa hoặc ghi đè dữ liệu chỉ để pull.
Sau đó mở lại backend và giao diện. `.env` và dữ liệu dự án cục bộ không được theo dõi bởi Git.

## D. Lỗi thường gặp

| Hiện tượng | Kiểm tra/cách xử lý |
| --- | --- |
| Không kết nối được 9Router | Máy chủ còn bật, 9Router còn chạy, IP/cổng đúng; chạy lại `Test-NetConnection` từ máy thành viên. |
| Cổng vẫn không thông | Cùng mạng chưa chắc cùng quyền truy cập: Wi-Fi khách/client isolation có thể chặn các máy. Kiểm tra mạng Private và tường lửa trên máy chủ. |
| Chuyển tiếp không chạy | Kiểm tra API gốc `127.0.0.1:20128`, dịch vụ `iphlpsvc` và `netsh interface portproxy show all`. |
| HTTP 401/403 | API key sai/hết hiệu lực hoặc không có quyền model. Dùng key của 9Router, không dùng token provider thay thế. |
| HTTP 404 | Kiểm tra base URL kết thúc bằng `/v1`, đúng ID model và model/provider có hỗ trợ endpoint tương ứng. |
| HTTP 429/quá quota | Nhiều người đang dùng chung tài khoản; giảm số lượt chạy đồng thời hoặc kiểm tra quota provider. |
| Model không đọc/sinh ảnh | Chọn model đúng khả năng; model chat không tự có chức năng sinh ảnh. |
| Backend không chạy | Chạy lệnh ở thư mục gốc, dùng đúng `.venv`, kiểm tra cổng 8000 chưa bị ứng dụng khác sử dụng. |
| Giao diện báo lỗi API | Backend phải đang chạy ở 8000; cấu hình Vite hiện chuyển `/api` tới `127.0.0.1:8000`. |
| Không tra được địa điểm/giá/bản đồ | Máy mỗi thành viên vẫn cần Internet để backend gọi nguồn dữ liệu công khai, không chỉ kết nối tới 9Router. |

## E. Ngừng chia sẻ 9Router khi không còn dùng

Nếu đã dùng cách chuyển tiếp 20129, chạy PowerShell bằng quyền administrator:

```powershell
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=20129
Remove-NetFirewallRule -DisplayName "TravelBook 9Router LAN proxy"
```

Nếu đã mở trực tiếp 20128 thì xóa quy tắc đã tạo:

```powershell
Remove-NetFirewallRule -DisplayName "TravelBook 9Router LAN"
```

Thu hồi các API key không còn cần trong 9Router.
