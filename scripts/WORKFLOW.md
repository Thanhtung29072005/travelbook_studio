# Một lệnh đi hết StateGraph từ JSON mẫu

Sau khi cài dependency (`python -m pip install -e ".[dev]"`), chạy từ thư mục gốc:

```powershell
python scripts/run_sample_workflow.py
```

Không cần LLM/9Router hoặc Internet. Đây là luồng fixture offline; không phải các
agent đã gọi model và không duyệt độ chính xác thực tế của kế hoạch du lịch.

## Đồ thị

PM -> Researcher -> Planner -> Budget -> Content Editor ->
{Image Curator, Illustrator, Map Agent} -> Reviewer -> Layout Editor -> PM xác nhận -> END.

Ba vai trò hình ảnh được dispatch bằng Send API; một barrier đợi cả ba trước Reviewer.
Artifact channel dùng reducer theo ID. Mỗi node trả cập nhật riêng, không trả lại toàn bộ State.
Có 10 vai trò agent và 11 lần thực thi node vì PM còn xác nhận ở cuối.
Đồ thị fixture chưa có vòng sửa lỗi, pause/resume hoặc checkpoint; các phần này sẽ
được bổ sung khi thay mock node bằng agent thực.

## Mỗi node làm gì

| Node | JSON đã đọc | Đầu ra |
| --- | --- | --- |
| PM | content, itinerary, budget | brief giả định của kịch bản và plan.json |
| Researcher | research | Place và research.json |
| Planner | itinerary | DayPlan, kiểm tra ID địa điểm |
| Budget | budget | Budget, tổng tính bằng Decimal |
| Content Editor | content | Fact nội dung |
| Image Curator | photos | Catalog ảnh, không tải ảnh placeholder |
| Illustrator | content | Prompt style lock và 4 PNG nền dự phòng |
| Map Agent | research, itinerary, budget | 3 sơ đồ tọa độ và 3 biểu đồ PNG |
| Reviewer | research, itinerary, budget, photos | Kiểm tra cấu trúc và fan-in; không duyệt dữ kiện |
| Layout Editor | cả 5 JSON | PDF từ JSON/PNG do các node trước tạo |
| PM xác nhận | content | Kiểm tra file PDF và hoàn tất demo |

Mỗi node ghi node_started, fixture_read (tên file + SHA-256), node_completed hoặc
node_failed vào SQLite. Mỗi artifact có source/confidence và được đăng ký vào DB.
State được lưu sau từng superstep; đây là snapshot để quan sát, chưa phải checkpoint để resume.

Review.verdict PASS ở bản demo chỉ có nghĩa **đạt kiểm tra cấu trúc luồng**.
State.production_approved luôn false; ảnh chưa xác minh không nhúng vào PDF.
Illustrator tạo placeholder để kiểm tra sản phẩm của nhánh; bản layout hiện không nhúng
các minh họa nền dự phòng này. Bản đồ là sơ đồ nối tọa độ, không phải tuyến đường thật.
PM dùng brief cố định Đà Nẵng/một người lớn của fixture, không phân tích prompt bằng LLM.

## Kết quả

Mỗi lần chạy tạo ID mới; dữ liệu nằm ở `backend/data/artifacts/<project_id>/`:
JSON đầu ra, illustrations/, maps/, charts/, review_reports/, travelbook.pdf,
travelbook.manifest.json và run_summary.json.
Database mặc định: backend/data/travelbook.db. Lệnh in đường dẫn PDF và số trang cuối cùng.
Nguồn JSON mẫu không bị ghi đè. ID dự án trùng hoặc thư mục đầu ra đã có dữ liệu sẽ bị từ chối.

```powershell
python scripts/run_sample_workflow.py --project-id demo-danang-01
python scripts/run_sample_workflow.py --sample-dir path/to/fixtures --output-dir output --database output/travelbook.db
python -m unittest discover -s tests -p test_workflow.py -v
```

Lỗi đọc/validate JSON hoặc tạo file sẽ chặn task liên quan, ghi sự kiện và đặt dự án failed.
Không đánh dấu completed khi thiếu nhánh hoặc không có PDF cuối.
