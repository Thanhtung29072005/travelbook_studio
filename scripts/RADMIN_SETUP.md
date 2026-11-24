# Dùng chung 9Router từ xa qua Radmin VPN

Mỗi thành viên chạy TravelBook trên máy riêng. Radmin VPN tạo kết nối riêng giữa các máy Windows, để backend TravelBook gọi 9Router trên máy trưởng nhóm.
Không cần cùng Wi-Fi, không cần cài 9Router/Codex trên máy thành viên và không cần mở cổng trên router Internet.

## 1. Cài Radmin VPN trên tất cả máy

Tải từ <https://www.radmin-vpn.com/> và cài trên máy trưởng nhóm lẫn máy các thành viên.
Chỉ cần **Radmin VPN** cho việc kết nối API này; không cần Radmin Server/Viewer để điều khiển màn hình.

## 2. Trưởng nhóm tạo mạng riêng

1. Mở Radmin VPN.
2. Chọn **Network → Create new network** (hoặc mục tạo mạng tương ứng với ngôn ngữ giao diện).
3. Đặt tên mạng, ví dụ `TravelBook-Team`, và mật khẩu riêng.
4. Gửi tên mạng/mật khẩu cho đúng thành viên qua kênh riêng.
5. Ghi lại địa chỉ IPv4 hiển thị trong Radmin VPN của máy bạn.

Ví dụ trong tài liệu này: IP Radmin của máy chủ là **26.10.20.30**.
Đây chỉ là ví dụ; phải thay bằng địa chỉ thực tế hiển thị trên máy bạn, không dùng IP Wi-Fi hoặc IP Internet.

## 3. Các thành viên tham gia mạng

1. Mở Radmin VPN.
2. Chọn **Network → Join an existing network**.
3. Nhập tên mạng và mật khẩu trưởng nhóm đã gửi.
4. Xác nhận nhìn thấy máy trưởng nhóm trong mạng và máy đó đang online.
5. Trưởng nhóm ghi lại IP Radmin của từng thành viên để cho phép truy cập API ở bước 4.

Ví dụ hai thành viên có IP **26.10.20.31** và **26.10.20.32**.
Trạng thái online trong Radmin chưa chứng minh cổng API đã mở; cần kiểm tra TCP ở bước 5.

## 4. Trưởng nhóm chia sẻ 9Router qua địa chỉ Radmin

### 4.1. Kiểm tra API gốc

Mở 9Router và kiểm tra tài khoản/provider, key và các model vẫn hoạt động.
Các lệnh sau giả sử API đang nghe tại **127.0.0.1:20128**. Nếu cài đặt của bạn dùng cổng khác, thay `20128` tương ứng.
Không lấy cổng trang quản trị thay cho cổng API.

Mở PowerShell và chạy:

```powershell
Test-NetConnection 127.0.0.1 -Port 20128
```

Cần thấy `TcpTestSucceeded : True`. Nếu False, xử lý API gốc trước khi chia sẻ VPN.

### 4.2. Chuyển tiếp cổng trên máy chủ

Nếu đã làm hướng dẫn LAN trước đó và có chuyển tiếp `0.0.0.0:20129`, thực hiện bước 4.3 để bỏ dòng cũ trước khi chạy các lệnh trong bước này.

Mở **PowerShell bằng Run as administrator** trên máy trưởng nhóm.
Thay các IP ví dụ bên dưới bằng IP Radmin thật:

```powershell
$routerVpnIp = "26.10.20.30"
$teamVpnIps = @("26.10.20.31", "26.10.20.32")

Start-Service iphlpsvc

netsh interface portproxy add v4tov4 listenaddress=$routerVpnIp listenport=20129 connectaddress=127.0.0.1 connectport=20128

New-NetFirewallRule -DisplayName "TravelBook 9Router Radmin" -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $routerVpnIp -LocalPort 20129 -RemoteAddress $teamVpnIps -Profile Any

netsh interface portproxy show all
```

Kết quả: thành viên truy cập **http://26.10.20.30:20129/v1**, Windows chuyển yêu cầu tới 9Router cục bộ ở cổng 20128.

- Cổng chia sẻ cho nhóm là **20129**, API gốc vẫn ở **20128**.
- Quy tắc chỉ cho các IP thành viên đã liệt kê truy cập địa chỉ Radmin này.
- Dùng `-Profile Any` vì adapter VPN có thể được Windows nhận là mạng Public; giới hạn vẫn nằm ở địa chỉ IP và cổng cụ thể.
- Quy tắc `LocalSubnet` trong hướng dẫn cùng Wi-Fi trước đó không thay thế quy tắc VPN này.
- Không tắt Windows Firewall và không cần chuyển toàn bộ adapter VPN thành Private.

### 4.3. Nếu trước đó đã tạo chuyển tiếp LAN trên cùng cổng 20129

Xem kết quả `netsh interface portproxy show all`. Nếu có dòng nghe ở **0.0.0.0:20129** do bạn đã làm theo hướng dẫn LAN, hãy xóa dòng đó trước khi tạo dòng nghe trên IP Radmin ở bước 4.2:

```powershell
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=20129
```

Lệnh chỉ xóa chuyển tiếp cũ này, không xóa dữ liệu TravelBook. Nếu đang có người dùng đường LAN cũ, việc xóa sẽ ngắt đường kết nối đó; thông báo cho họ chuyển sang địa chỉ Radmin.

## 5. Thành viên kiểm tra kết nối từ máy mình

Radmin VPN phải đang bật trên cả hai máy. Mở PowerShell trên máy thành viên:

```powershell
Test-NetConnection 26.10.20.30 -Port 20129
```

Cần thấy **TcpTestSucceeded : True**. Nếu ping không phản hồi nhưng TCP True thì API vẫn có thể dùng; ping có thể bị tường lửa chặn riêng.

Sau đó thử API bằng key 9Router trưởng nhóm cấp. Nhập key khi được hỏi:

```powershell
$teamRouterKey = Read-Host "API key 9Router" -AsSecureString
$teamRouterCredential = [System.Net.NetworkCredential]::new("", $teamRouterKey)
$teamRouterHeaders = @{ Authorization = "Bearer " + $teamRouterCredential.Password }
Invoke-RestMethod -Uri "http://26.10.20.30:20129/v1/models" -Headers $teamRouterHeaders
Remove-Variable teamRouterKey, teamRouterCredential, teamRouterHeaders
```

Nếu bản 9Router đang cài không có endpoint `/v1/models`, dùng chức năng thử kết nối/model trong TravelBook hoặc 9Router để kiểm tra.
Danh sách model không bảo đảm tất cả model còn quota hoặc có khả năng sinh ảnh.

## 6. Cấu hình TravelBook trên máy mỗi thành viên

Phần cài Git/Python/Node.js, tải mã nguồn và cài thư viện giữ nguyên trong [TEAM_SETUP.md](TEAM_SETUP.md), mục B.
Trong file **.env ở thư mục gốc TravelBook**, sửa các dòng có sẵn:

```dotenv
NINE_ROUTER_BASE_URL=http://26.10.20.30:20129/v1
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

Điền key và các ID model thực tế trưởng nhóm cung cấp sau dấu `=` ở các dòng đang để trống.
Không thêm nhiều dòng trùng tên biến. Không dùng `localhost` cho base URL 9Router trên máy thành viên, vì nó trỏ về chính máy đó.

Máy trưởng nhóm nếu cũng chạy TravelBook có thể tiếp tục dùng **http://127.0.0.1:20128/v1** trong `.env` của mình.

Giữ key trong `.env`, không đưa vào `.env.example` hoặc GitHub.
Mật khẩu mạng Radmin và API key 9Router là hai thông tin khác nhau: tham gia VPN không thay thế xác thực API.

## 7. Chạy TravelBook

Trên máy từng thành viên, mở PowerShell tại thư mục gốc dự án và chạy backend:

```powershell
.\.venv\Scripts\python.exe -m backend.app.main
```

Mở cửa sổ PowerShell thứ hai, cũng từ thư mục gốc:

```powershell
cd frontend
npm.cmd run dev
```

Mở **http://localhost:5173**, vào **Model 9Router** kiểm tra cấu hình đã lưu và thử kết nối trước khi tạo dự án.
Giao diện TravelBook vẫn mở bằng localhost trên máy mỗi người; chỉ base URL 9Router dùng IP Radmin.
Sau khi thay `.env`, dừng backend bằng `Ctrl+C` rồi chạy lại để nhận cấu hình mới.

## 8. Khi có thành viên mới

Sau khi người đó tham gia mạng Radmin, thêm IP thực tế của họ vào danh sách được phép. Chạy trên máy chủ bằng PowerShell administrator; liệt kê đầy đủ các thành viên còn được cấp quyền:

```powershell
$teamVpnIps = @("26.10.20.31", "26.10.20.32", "26.10.20.33")
Get-NetFirewallRule -DisplayName "TravelBook 9Router Radmin" | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress $teamVpnIps
```

Nếu có người rời nhóm, bỏ IP của họ khỏi danh sách, thu hồi API key riêng nếu có và xử lý quyền tham gia mạng/mật khẩu Radmin tương ứng.

## 9. Lỗi thường gặp

| Hiện tượng | Cách kiểm tra |
| --- | --- |
| Không thấy máy chủ hoặc máy chủ offline | Hai máy bật Radmin, vào đúng mạng, máy chủ còn bật và có Internet. |
| TCP 20129 False | Kiểm tra IP Radmin/cổng; IP thành viên nằm trong danh sách firewall; chuyển tiếp đúng IP; `iphlpsvc` đang chạy. |
| TCP gốc 20128 False | 9Router chưa chạy, API dùng cổng khác hoặc không lắng nghe trên IPv4 localhost. Sửa API gốc trước. |
| Chuyển tiếp không hoạt động | Chạy `netsh interface portproxy show all`; kiểm tra `Get-Service iphlpsvc`; tránh dòng wildcard cũ trùng cổng 20129. |
| TCP True nhưng HTTP 401/403 | API key sai/hết hiệu lực/không có quyền; không phải lỗi VPN. |
| HTTP 404 | Kiểm tra đường dẫn kết thúc `/v1`, ID model và endpoint API được model/provider hỗ trợ. |
| HTTP 429 hoặc nhiều lần chờ lâu | Các thành viên dùng chung quota; thử một người chạy trước, rồi tăng số lượt đồng thời. |
| Đổi .env nhưng vẫn gọi IP cũ | Khởi động lại backend và kiểm tra cấu hình đã lưu trong trang Model 9Router. |
| Thỉnh thoảng mất kết nối | Kiểm tra máy chủ Sleep, mạng của hai bên và trạng thái Radmin. |

## 10. Điều kiện để dùng ổn định

- Máy chủ phải bật, có Internet, Radmin VPN và 9Router đang chạy; máy thành viên cũng phải bật Radmin VPN khi dùng AI.
- Máy thành viên vẫn cần Internet để TravelBook tra cứu địa điểm, thời tiết, giá và bản đồ.
- Chi phí/quota model tính trên tài khoản/provider dùng chung ở máy chủ, theo chính sách của provider.
- Chỉ cấp quyền cho thành viên tin cậy, dùng API key riêng nếu 9Router hỗ trợ. Không chia sẻ trang quản trị 9Router nếu không cần.
- Không mở cổng này trực tiếp ra Internet bằng cấu hình port forwarding trên modem/router.
- Mô hình này chia sẻ AI, không đồng bộ dự án/PDF giữa các máy. Muốn cùng xem một dự án cần phương án chạy TravelBook tập trung riêng.

## 11. Ngừng chia sẻ

Trên máy chủ, PowerShell administrator; thay IP bằng IP Radmin thật:

```powershell
netsh interface portproxy delete v4tov4 listenaddress=26.10.20.30 listenport=20129
Remove-NetFirewallRule -DisplayName "TravelBook 9Router Radmin"
```

Thu hồi key không còn dùng và ngắt/xử lý quyền tham gia mạng Radmin khi cần.
