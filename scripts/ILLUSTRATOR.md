# Illustrator: bộ ảnh theo style lock

```powershell
python scripts/check_illustrator.py
python scripts/check_illustrator.py --offline
python scripts/check_illustrator.py --mock
python scripts/check_illustrator.py --input-dir path/to/json --trip-request path/to/trip.json
```

Lệnh mặc định gọi Images API qua 9Router theo cấu hình .env. --offline tạo nền
dự phòng hoàn toàn, không gọi mạng. --mock kiểm tra HTTP bằng hình khối màu tổng
hợp, không phải ảnh AI thật. CLI tạo các task phụ thuộc ở trạng thái demo done;
không thực thi Content Editor hoặc các agent trước đó trong lệnh này.

## Bộ ảnh

Với lịch Đà Nẵng 3 ngày, đầu ra gồm 10 PNG:

| Nhóm | Số ảnh | Kích thước đầu ra |
| --- | ---: | --- |
| cover | 1 | 1024 × 1536 |
| day_1..day_N | Mỗi ngày 1 | 1536 × 1024 |
| icon_location, icon_food, icon_transport, icon_culture | 4 | 256 × 256 |
| background_waves, background_dots | 2 | 1024 × 1024 |

Icon được tạo ở kích thước API hỗ trợ rồi resize/crop bằng Pillow. Đây là ảnh
RGB nền đục, không cam kết icon trong suốt hoặc nền ghép nối liền mạch.
Ảnh chỉ minh họa trang trí, không thay thế ảnh thật, bản đồ hoặc biểu đồ dữ liệu.
Tên điểm/ngày được đưa vào brief từ TripRequest/DayPlan/Place, không tự bịa lịch.

Mọi prompt được chèn cùng StyleLock bất biến, gồm mô tả, bảng màu và quy tắc
không sinh chữ/logo/watermark. Agent chụp cấu hình style một lần cho toàn bộ bộ
ảnh. IMAGE_PALETTE có 2..8 màu RGB, màu đầu dùng làm nền dự phòng. Mặc định:
ivory #F3EBDD, teal #247C82, terracotta #C76D4D. IMAGE_STYLE_LOCK cho phép thay
mô tả phong cách. SHA256 bao gồm cả mô tả, bảng màu và quy tắc.

Một chữ ký style/prompt chung là kiểm chứng hợp đồng đầu vào, không phải phép
đánh giá tự động rằng mọi nét vẽ đều đồng nhất. contact_sheet.png dùng để xem
lại cả bộ; bước Reviewer vẫn cần đánh giá hình ảnh. production_approved=false.

## Model, cache và lỗi

- NINE_ROUTER_IMAGE_MODEL: model chính từ catalog của 9Router đang chạy.
- NINE_ROUTER_IMAGE_FALLBACK_MODELS: danh sách ID dự phòng, phân cách dấu phẩy.
- NINE_ROUTER_API_KEY / NINE_ROUTER_BASE_URL: dùng cấu hình router hiện có.
- IMAGE_TIMEOUT_SECONDS: thời gian chờ mỗi yêu cầu, mặc định 180 giây.
- IMAGE_CACHE_DIR: cache PNG theo hash prompt + model + tài khoản + size + style.
- IMAGE_DOWNLOAD_HOSTS: host HTTPS cho phép nếu provider trả URL thay vì base64.

Adapter gọi POST /images/generations với n=1 và response_format=b64_json theo
cách triển khai image service của source mẫu. Chỉ dùng model hỗ trợ endpoint này;
model chat hoặc ảnh trong chat/completions chưa được adapter này hỗ trợ.
Nếu trả URL, chỉ nhận cùng origin 9Router hoặc host HTTPS đã cho phép, không
theo redirect và không chuyển Authorization khi tải ảnh. Giới hạn bytes/pixel,
kiểm tra ảnh bằng Pillow, chuẩn hóa đúng kích thước và lưu PNG.

Thiếu model/key, lỗi provider, base64 hỏng hay file không đọc được: dùng ảnh nền
màu đầu palette, đánh dấu is_placeholder=true và warning. Không ngụy trang nền
này thành icon hoặc ảnh AI đã sinh thành công. Placeholder không được cache;
lần chạy sau có thể tạo lại. Khi tất cả model gặp lỗi HTTP/kết nối, agent dùng
dự phòng cho phần còn lại của bộ thay vì lặp lại cùng lỗi mạng cho mọi ảnh.
Lỗi cache ghi được cảnh báo nhưng vẫn giữ ảnh đã tạo; lỗi ghi artifact/DB làm
task thất bại để không báo hoàn thành khi chưa có file.

## Artifact và tích hợp

Mỗi lần chạy có thư mục riêng dưới backend/data/artifacts/<project_id>/illustrator/<run_id>.
PNG được đăng ký SQLite Artifact(kind=illustration). illustrations.json theo
IllustrationSet có role/ngày, kích thước, prompt, style signature, SHA256 file,
model, cache_hit, is_placeholder và warnings. Dữ kiện dùng source/confidence.
CLI xuất thêm contact_sheet.png và illustrator_validation.json.

IllustratorAgent.run(State) nhận project_id, trip_request, itinerary, places;
task Content Editor phụ thuộc phải done. State.illustrations chứa các Artifact
PNG, State.artifacts thêm cả manifest, State.illustration_warnings giữ cảnh báo.
Task Illustrator vẫn done khi dùng dự phòng hợp lệ theo yêu cầu. Chỉ mở Reviewer
khi Image Curator, Illustrator và Map Agent đều done; giữ needs_attention nếu
nhánh ảnh thật hoặc bản đồ đang blocked.

workflow.py và LayoutService hiện chưa tự sử dụng bộ minh họa mới: đây là agent
và bộ artifact để nối vào luồng thật ở bước sau. JSON mẫu không bị ghi đè.

```powershell
python -m unittest discover -s tests -v
```

Kiểm thử gồm đủ bìa/ngày/icon/nền, đúng size/PNG, một style signature, model
fallback, ảnh hỏng, URL tải không hợp lệ, cache, lỗi mạng, nhiệm vụ và sự kiện DB.

Lần kiểm chứng thực tế: 9Router đã tạo đủ 10/10 PNG cho Đà Nẵng 3 ngày, gồm
1 bìa, 3 ảnh mở đầu ngày, 4 icon, 2 nền; 0 placeholder, tất cả đúng kích thước
và cùng style signature. Đã xem contact sheet để kiểm tra sự thống nhất màu
nước, nền kem, teal và terracotta. Toàn bộ 142 kiểm thử đạt, gồm các ca lỗi cần
ảnh dự phòng. Kết quả trong thư mục illustrator-live-0cbd4b795b4b đi kèm gói mã.
