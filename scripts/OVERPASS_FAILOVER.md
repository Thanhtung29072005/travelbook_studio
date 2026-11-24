# Overpass HTTP 502 ở dự án Nha Trang

Đã xác định từ DB dự án `project-bb1a20177c47444fbff3ae6d50e75e5f`: Nominatim geocode thành công, Researcher không có places và cả hai cảnh báo là `overpass: HTTP 502`. LLM PM/Researcher đã trả structured output; không có bằng chứng lỗi model ở bước dừng này.

Bổ sung `OVERPASS_FALLBACK_URLS` mặc định `https://overpass.private.coffee/api/interpreter`. Đây là instance global được liệt kê tại https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances ; danh sách không bảo đảm server luôn sẵn sàng.

Chỉ thử tuần tự từng endpoint một lần khi lỗi kết nối/JSON không hợp lệ hoặc HTTP 500/502/503/504. Không xoay server để vượt 429 hay che lỗi truy vấn 4xx. Kết quả hợp lệ nhưng rỗng không bị coi là lỗi server; không tạo địa điểm giả. Timeout query được đặt thấp hơn timeout HTTP mặc định. Giữ cache/rate-limit của PublicAPIClient.

Sự kiện `data_failover` ghi host cũ/mới và HTTP status; nguồn tọa độ giữ OSM ID, thời điểm lấy dữ liệu và host trả kết quả. Nếu mọi endpoint lỗi, `research_diagnostics.json` giữ cảnh báo cụ thể.

Sau cập nhật: khởi động lại backend rồi bấm **Tiếp tục** ở dự án Nha Trang. Không cần tạo lại dự án hay xóa DB/cache. `.env` cũ không cần sửa vì fallback có giá trị mặc định. Có thể cấu hình danh sách endpoint bằng dấu phẩy, tối đa 3 dự phòng, hoặc đặt rỗng để tắt.

Kiểm tra cú pháp Python đã chạy; 5 test mới trong `tests/test_overpass_failover.py` đã viết, chưa chạy vì các lượt test/backend trước bị Auto-review chặn telemetry ngoài dự kiến. Chưa xác minh fallback lấy dữ liệu thật trên máy đang chạy ứng dụng. Lệnh test khi môi trường cho phép:

```powershell
python -m unittest discover -s tests -p test_overpass_failover.py -v
```
