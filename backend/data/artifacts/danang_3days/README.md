# Đà Nẵng 3 ngày — fixture offline

Dữ liệu mẫu phục vụ kiểm thử schema và workflow, chưa xác minh thực tế.
Giả định: một người lớn, ba ngày hai đêm, ngân sách tại điểm đến 5.000.000 VND.
Không bao gồm chi phí đến/rời Đà Nẵng và mua sắm cá nhân.

| File | Kiểu schema khi đọc |
| --- | --- |
| research.json | `list[Place]` (nạp vào `State.places`) |
| itinerary.json | `list[DayPlan]` (nạp vào `State.itinerary`) |
| budget.json | `Budget` (nạp vào `State.budget`) |
| content.json | `list[Fact[str]]` (nạp vào `State.content`) |
| photos.json | `list[Photo]` (nạp vào `State.photos`) |

Mỗi dữ kiện có `value`, `source`, `confidence`; ID, số thứ tự ngày và đường dẫn là metadata.
`source` có tiền tố `fixture:` vì không có API nào đã được gọi để xác minh dữ liệu này.
Tọa độ và thời gian di chuyển chỉ là giá trị xấp xỉ trong fixture.
Giờ mở cửa, giá vé từng điểm, ngày đi và dự báo thời tiết chưa biết nên để null hoặc danh sách rỗng.
Điểm ngắm cảnh Sơn Trà là điểm giả lập, không phải địa điểm đã xác minh.

Tất cả chi phí có `price_kind: estimated`. Tổng tính bằng Decimal: **3.700.000 VND**;
phần còn lại so với giới hạn: **1.300.000 VND**.
`total` và `within_budget` là computed fields của Budget: không lưu vào JSON đầu vào
để file đọc lại được bằng `Budget.model_validate_json(...)` với `extra=forbid`.
Sau khi đọc, dùng `budget.total` và `budget.within_budget`; `model_dump_json()` sẽ xuất cả hai.

Ảnh là placeholder trên miền `example.invalid`, không truy cập được.
Tác giả, giấy phép, URL ảnh và URL nguồn có confidence bằng 0.
Không tải hoặc sử dụng ảnh trong PDF thật cho đến khi Image Curator thay bằng dữ liệu đã xác minh.

Từ thư mục gốc, dùng Python có Pydantic 2 để sinh lại và kiểm tra 5 file:

```powershell
python scripts/generate_danang_sample.py
```

Lệnh kiểm tra schema trên JSON đã ghi, ID liên kết, ba ngày liên tiếp và tổng ngân sách.
