# Dàn trang PDF bản đầu

```powershell
python -m pip install -e ".[dev]"
python scripts/check_layout.py
python -m unittest discover -s tests -p test_layout.py -v
```

Đầu vào là 5 JSON mẫu trong backend/data/artifacts/danang_3days và các PNG
đã tạo ở maps/ và charts/. Nếu thiếu PNG, chạy scripts/check_maps_charts.py trước.

```python
from backend.app.services.layout import LayoutService

pdf_path = LayoutService().build_from_json("backend/data/artifacts/danang_3days")
```

Kết quả: travelbook.pdf, travelbook.manifest.json và (khi chạy check_layout.py)
layout_validation.json. Manifest ghi số trang, trang bắt đầu các phần, nguồn,
confidence thấp nhất, ảnh được nhúng và ảnh bị loại.

## PDF mẫu 11 trang

1. Bìa, thời lượng, ngân sách và nhãn dữ liệu demo.
2. Tổng quan, nội dung biên tập và các lưu ý trong content.json.
3. Ngày 1: lịch trình, bản đồ và ghi chú độ tin cậy.
4. Ngày 2: lịch trình, bản đồ và ghi chú độ tin cậy.
5. Ngày 3: lịch trình, bản đồ và ghi chú độ tin cậy.
6. Ngân sách: biểu đồ, tổng chi phí và bảng khoản chi.
7. Thời tiết: biểu đồ trạng thái chưa có dự báo, không tự tạo số liệu.
8. Timeline toàn chuyến.
9. Nguồn ảnh và giấy phép, nhóm 1.
10. Nguồn ảnh và giấy phép, nhóm 2.
11. Nguồn dữ liệu và giới hạn.

Nội dung dài tự phân trang; 11 là số trang của fixture hiện tại, không phải giới hạn.
Mỗi trang có footer “Trang X / Y”. Font TrueType Unicode được nhúng để giữ tiếng Việt.
Tự tìm Arial trên Windows hoặc DejaVu Sans trên Linux; có thể truyền font_regular/font_bold.

## API dữ liệu trực tiếp

LayoutService.build_pdf(output_path, *, days, places, budget, content, photos, maps, charts,
title="Cẩm nang du lịch", image_root=None) nhận các model hoặc dict đúng schema.
maps là dict day_number -> đường dẫn PNG; charts có budget/weather/timeline -> đường dẫn PNG.
Asset bắt buộc được kiểm tra trước khi ghi; PDF được tạo ra file tạm rồi thay thế khi thành công.
Không tải ảnh từ Internet. local_path tương đối được giải theo image_root nếu được cung cấp.

Ảnh chỉ được nhúng khi có file local, metadata tác giả/giấy phép/URL có confidence > 0,
và license không mang nhãn UNVERIFIED/PLACEHOLDER. Đây là điều kiện kỹ thuật dựa trên
metadata đầu vào, không thay thế bước kiểm tra quyền sử dụng của Image Curator/Reviewer.
Ảnh JSON mẫu có confidence 0 nên chỉ xuất trong trang nguồn, không đưa vào lịch trình.

Bản đồ vẫn là sơ đồ tọa độ offline, không phải chỉ đường. PDF ghi rõ giới hạn dữ liệu demo.
Chữ PDF do ReportLab xử lý; không raster hóa văn bản thành ảnh.
