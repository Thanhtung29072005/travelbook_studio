# Duyệt thủ công bằng interrupt

Workflow gọi `langgraph.types.interrupt` sau Planner (lịch trình), sau khi ba agent hình ảnh đã gộp và Reviewer đã chạy (ảnh), và sau Layout Editor (PDF). Có thể bật thêm điểm duyệt kế hoạch PM. Mỗi điểm đều có checkpoint SQLite; quyết định dùng `Command(resume={interrupt_id: payload})` trên cùng thread, không chạy lại từ START.

## Trên UI

1. Khi tạo dự án, bật **Duyệt thủ công**; hoặc cấu hình các giai đoạn trong bảng duyệt trước khi chạy.
2. Khi trạng thái là **Chờ duyệt**, mở lịch trình, ảnh với tác giả/giấy phép/nguồn, hoặc liên kết PDF.
3. **Duyệt và đi tiếp** tiếp tục từ điểm interrupt, không chạy lại agent vừa tạo nội dung.
4. **Yêu cầu sửa** bắt buộc có phản hồi. Lịch trình về Planner; ảnh cho chọn Image Curator, Illustrator hoặc Map Agent; PDF về Layout Editor. Sau khi sửa sẽ xuất hiện yêu cầu duyệt mới.

Mỗi vòng sửa dùng thư mục `executions/N`, giữ nguyên file và artifact đã duyệt trước đó. Phản hồi nằm trong `State.feedback` với nguồn `user:approval:ID` và confidence 1. Agent tiếp theo nhận State này. Quyết định có ID không khớp checkpoint, ID đã xử lý, phản hồi trống hoặc agent sai giai đoạn bị từ chối. Nút khóa khi đang chạy/gửi quyết định.

Không tắt cổng đã mở bằng cách bỏ chọn cấu hình: cần xử lý quyết định đang chờ. Thiết lập chỉ áp dụng cho điểm duyệt chưa mở. Phản hồi của người dùng độc lập với số vòng sửa tự động `MAX_REVISIONS`; người dùng có thể yêu cầu thêm vòng thủ công.

## API

- `GET /api/projects/{id}/approvals`: các vòng duyệt và nội dung được chụp tại thời điểm duyệt.
- `GET /api/projects/{id}/checkpoint`: next_nodes và interrupts (ID, stage, approval_id).
- `POST /api/projects/{id}/approvals/{approval_id}`:

```json
{"decision":"revise","feedback":"Đổi ảnh mở đầu ngày 2","target_agent":"illustrator"}
```

Thay bằng `{"decision":"approve"}` để đi tiếp. Request được nhận trước khi chạy nền; xem trạng thái và sự kiện tiếp theo để biết kết quả thực thi. Bảng approvals chỉ ghi quyết định đã tiêu thụ sau khi checkpoint được lưu; có thể khôi phục bản ghi này từ State nếu tiến trình dừng giữa hai thao tác.

## Kiểm tra

```powershell
python scripts/check_manual_approvals.py
python -m unittest discover -s tests -p test_manual_approvals.py -v
```

Demo mặc định thử sửa rồi duyệt ở cả ba điểm và xuất PDF + `manual_approval_validation.json`. Mỗi lần quyết định tạo manager/checkpointer mới để đọc lại SQLite. Kiểm tra hai tiến trình riêng:

```powershell
python scripts/check_manual_approvals.py --pause-only
# Lấy project_id vừa in, chạy lệnh ở tiến trình mới:
python scripts/check_manual_approvals.py --resume PROJECT_ID --decision revise --target-agent planner --feedback "Điều chỉnh giờ khởi hành"
python scripts/check_manual_approvals.py --resume PROJECT_ID --decision approve
# Tiếp tục --resume để duyệt ảnh, sau đó PDF.
```

Các kiểm tra ASGI thử cả hai quyết định, ba agent ảnh, chống gửi trùng, sai agent, tải PDF, và kiểm tra file cũ không bị ghi đè. Không chạy server/browser trực tiếp trong phiên kiểm tra này. Graph hiện dùng JSON mẫu: kiểm tra được điều phối/duyệt/tạo lại đầu ra; chưa chứng minh agent LLM thật thay nội dung theo phản hồi. Ảnh mẫu/minh họa dự phòng và dữ kiện chưa xác minh vẫn được đánh dấu; duyệt thủ công không đổi `production_approved` thành true.
