# Tích hợp agent thật vào UI

API và WorkflowManager mặc định khởi tạo `LiveTravelBookWorkflow`. Workflow mẫu `TravelBookWorkflow` vẫn được giữ để chạy demo/test một cách tường minh. Adapter live dùng lại StateGraph, wrapper sự kiện/task, SQLite checkpointer, Send/fan-in, interrupt duyệt thủ công và RevisionController hiện có; nó không đọc các file mẫu được khai báo trong graph demo.

## Sau khi cập nhật code

1. Dừng backend đang chạy, rồi khởi động lại bằng `python -m backend.app.main`. Đảm bảo đã cài dependencies (`python -m pip install -e ".[dev]"`).
2. Kiểm tra 9Router đang chạy, API key và chuỗi model trong `.env`/trang Model. PM và Content Editor cần model phản hồi structured JSON. Reviewer vision cần model hỗ trợ ảnh; Illustrator cần model hỗ trợ image generation, nếu không có sẽ ghi rõ ảnh nền dự phòng.
3. **Tạo dự án mới**, ví dụ: “Đi Phú Quốc 3 ngày cho 2 người lớn, tổng ngân sách 10 triệu đồng, thích biển và văn hóa địa phương”. Dự án demo cũ vẫn giữ nguyên dữ liệu và nhãn demo; retry/resume không chuyển chúng thành dữ liệu thật.
4. Nếu thiếu số ngày, số người hoặc ngân sách/tiền tệ, PM dừng bằng interrupt. Trả lời ô **PM cần bổ sung thông tin → Gửi và tiếp tục**. Không điền mặc định Đà Nẵng/3 ngày/1 người.
5. Theo dõi Giao tiếp và Sản phẩm để xem `data_request`, `model_call`, `node_completed`, JSON, ảnh và PDF. Bật duyệt thủ công nếu muốn kiểm tra lịch trình/ảnh/PDF trước khi đi tiếp.

## Agent được nối

| Bước | Xử lý thực tế |
| --- | --- |
| PM | `PMAgent.normalize`, bằng chứng từ yêu cầu, hỏi lại thông tin còn thiếu |
| Researcher | `ResearcherAgent.research`, Nominatim/Overpass/Wikidata, Open-Meteo |
| Planner | `PlannerAgent.plan`, routing OSRM/ORS/haversine có đánh dấu ước tính |
| Budget | `BudgetAgent.calculate`, Decimal; giá vé có nguồn và hạn mức ước tính công khai |
| Content | `ContentEditorAgent.edit`, prose theo evidence đầu vào |
| Image Curator | `ImageCuratorAgent.curate`, nguồn/license; tải ảnh Wikimedia/Pexels qua allowlist, giới hạn dung lượng/pixel |
| Illustrator | `IllustratorAgent.create_set`, style lock, ảnh dự phòng có cảnh báo |
| Map | `MapAgent.render`, đọc JSON riêng của dự án/vòng sửa hiện tại |
| Reviewer | luật code + qualitative/vision; giữ lỗi thiếu bằng chứng |
| Layout | `LayoutEditorAgent.render`, bản đồ/ảnh/nội dung hiện tại, nguồn và cảnh báo vào PDF |

Các agent không tự thay trạng thái toàn dự án bằng các hàm `run` độc lập trong lúc graph đang điều phối. Adapter gọi phương thức xử lý của từng agent và để manager quản lý checkpoint/pause/resume. Ba nhánh visual không ghi chung stage/error/fixture_mode; artifacts gộp bằng reducer.

Không tìm được địa điểm hoặc không lập được đủ ngày: dừng và báo lỗi, không thay bằng fixture. Không có ngày đi: thời tiết ghi `not_requested`. Không có ảnh thật phù hợp: ảnh vẫn thiếu, có cảnh báo và Reviewer xem xét; không tự tạo metadata/license giả. Unsplash giữ link/attribution, chưa nhúng ảnh hotlink vào PDF. JPG tải về được kiểm tra, chuyển JPEG preview và lưu trong thư mục dự án.

## Ngân sách và giới hạn

Hạn mức VND mặc định là **ước tính cấu hình**, không phải giá thị trường vừa tra cứu:

```dotenv
LIVE_FOOD_VND_PER_PERSON_DAY=200000
LIVE_TRANSPORT_VND_PER_DAY=250000
LIVE_HOTEL_VND_PER_ROOM_NIGHT=600000
```

Giả định 2 người/phòng, số đêm = số ngày − 1. Không bao gồm vé di chuyển đến điểm đến và vé tham quan chưa biết giá. Tiền tệ khác không được tự quy đổi hay áp dụng giá VND. `budget_warnings` và các cảnh báo dữ liệu/ảnh đi vào PDF. Graph hỗ trợ tối đa 31 ngày. Dữ liệu bên ngoài còn phụ thuộc coverage, mạng, rate limit và quyền truy cập model/provider.

PDF hoàn tất là bản nháp để kiểm tra; `production_approved` không tự đổi thành true. Hết `MAX_REVISIONS` vẫn giữ Review REVISE/cảnh báo và xuất bản nháp theo chính sách hiện tại. Việc gọi agent thật không biến mọi dữ kiện thành đã xác minh.

## Kiểm chứng

```powershell
python -m unittest discover -s tests -p test_live_workflow.py -v
python scripts/run_live.py --prompt "Đi Phú Quốc 3 ngày cho 2 người lớn, tổng ngân sách 10 triệu đồng"
```

Bộ 4 test mới kiểm tra: thiếu model phải lỗi thay vì ra mẫu; destination truyền đến Researcher/Planner và PDF; PM hỏi rồi tiếp tục cùng checkpoint; chặn resume demo cũ. Test end-to-end dùng mock provider/LLM nhưng graph/DB/PNG/PDF thật. Các test demo workspace và manual approval chuyển sang inject workflow fixture tường minh.

Trong phiên triển khai này chỉ chạy kiểm tra cú pháp Python và production build frontend. Chưa chạy bộ integration hoặc gọi provider thật vì những lượt chạy backend/preview trước bị Auto-review chặn do telemetry tới dịch vụ Visual Studio với payload không rõ. Không khởi chạy lại backend/preview qua cơ chế khác để vượt chặn. Cần kiểm chứng các lệnh trên trong môi trường chạy ứng dụng trước khi khẳng định kết quả live end-to-end.
