# Planner: gom cụm và xếp lịch

Chạy từ thư mục gốc với Python đã cài dependency trong pyproject.toml:

```powershell
python scripts/check_planner.py --offline
python scripts/check_planner.py --start-date 2026-12-07 --ages 35,34,5
python scripts/check_planner.py --research path/to/research.json --trip-request path/to/trip.json
```

Đầu vào research.json là danh sách Place; trip.json là TripRequest. Mặc định dùng
JSON mẫu Đà Nẵng 3 ngày, chưa xác minh dữ liệu địa điểm. --ages phải cung cấp tuổi
của toàn nhóm; CLI cập nhật travelers theo số tuổi được nhập. --mock dùng phản hồi
HTTP giả để kiểm tra giao thức; --offline chỉ dùng haversine. Không ghép hai cờ này.

Mỗi lần chạy tạo dự án SQLite riêng, itinerary.json, planner_report.json và
planner_validation.json dưới backend/data/artifacts/<project_id>/. Mã thoát 0 khi
đủ ngày, 2 khi thiếu ngày. Mỗi DayPlan có stops theo thứ tự; arrival/departure là
giờ địa phương, travel_minutes là chặng từ điểm trước cộng buffer chuyển tiếp.
Mọi giá trị nghiệp vụ có source/confidence; báo cáo giữ ma trận tuyến và lý do
không chọn điểm. Điểm đầu mỗi ngày có travel_minutes=0 vì phạm vi bắt đầu tại
điểm đó, chưa bao gồm chặng đi/về nơi ở.

## Routing

RoutingService thử OSRM -> ORS -> haversine, cấu hình trong .env.example.
ORS cần OPENROUTESERVICE_API_KEY. OSRM/ORS dùng thứ tự tọa độ longitude,latitude;
chuyển giây thành phút làm tròn lên và mét thành km. Cache SQLite theo request,
gồm hash header để tách khóa xác thực, không lưu khóa vào source/cache body.

Tài liệu giao thức:
- https://project-osrm.org/docs/v26.6.1/http.html
- https://giscience.github.io/openrouteservice/api-reference/endpoints/matrix/

Walking/cycling chỉ gọi OSRM khi có profile tương ứng được cấu hình cho server.
Không dùng thời gian driving cho walking. Lỗi mạng hoặc dữ liệu lỗi chuyển
provider; cặp null/NoRoute/NoTable/NoSegment chỉ được cứu bằng một provider
đường bộ khác. Nếu vẫn không có đường, đánh dấu unreachable, không tạo thời
gian hữu hạn bằng haversine cho cặp đó.

Haversine dùng khoảng cách cầu x hệ số 1.35 và tốc độ giả định: ô tô 25, đi bộ 4,
xe đạp 12 km/h. Có thể cấu hình lại. Đây là ước tính, confidence tối đa 0.25,
không xác nhận đường thực tế, giao thông hay vật cản. Đây là ma trận di chuyển,
chưa phải hình học tuyến đường để vẽ bản đồ.

## Chính sách xếp lịch

- Gom điểm theo tọa độ bằng farthest-first và cân bằng số điểm, không lặp điểm.
  Ưu tiên cụm của ngày, sau đó điểm có giờ đóng cửa sớm và chi phí chuyển/chờ thấp.
  Đây là heuristic, chưa bảo đảm tối ưu toàn cục.
- Mặc định 09:00-17:00, tối đa 4 điểm/ngày, buffer 10 phút. Không xếp chuyến
  tham quan hay di chuyển qua giờ nghỉ 12:00-13:00. Giữ thời lượng tham quan từ
  dữ liệu; thiếu thời lượng thì dùng giả định có ghi nguồn.
- Có trẻ dưới 12 tuổi/người từ 65 tuổi hoặc yêu cầu nhịp nhẹ: tối đa 3 điểm,
  tổng tham quan + di chuyển tối đa 240 phút, thêm 5 phút buffer. Đây là chính
  sách lịch trình, không phải tư vấn sức khỏe.
- minimum_age/maximum_age của Place là giới hạn cứng. Thiếu tuổi toàn nhóm thì
  không chọn điểm bị giới hạn tuổi. age_notes hỗ trợ min_age/max_age, adults only,
  “chỉ người lớn”, một số cảnh báo trẻ em/người cao tuổi; chưa hiểu mọi văn bản tự do.
- Parser giờ mở cửa hỗ trợ 24/7, HH:MM-HH:MM, Mo-Fr, danh sách ngày, nhiều khoảng
  trong ngày và các mệnh đề phân cách dấu chấm phẩy, off/closed. Không hỗ trợ
  qua đêm, ngày lễ, lịch theo mùa hoặc biểu thức OSM phức tạp: loại điểm và ghi lý do.
  Thiếu giờ mở cửa hoặc thiếu ngày để xác định thứ: lịch tạm tính có cảnh báo.
- Không đủ điểm/khung giờ để lấp mọi ngày: lưu lịch hiện có, missing_days và
  needs_attention, chặn Budget. Khi đủ ngày, đánh dấu task Planner done và mở
  Budget ready; production_approved vẫn false để giữ bước kiểm duyệt.

PlannerAgent.run nhận State chứa project_id, trip_request và places; dự án cần
được tạo trước, các phụ thuộc Researcher phải done. Sự kiện, task và artifact
được ghi DB qua BaseAgent. workflow.py hiện vẫn là đồ thị fixture, không tự
chuyển sang agent thật chỉ vì thêm module này.

## Kiểm chứng

```powershell
python -m unittest discover -s tests -v
```

Kiểm thử gồm giao thức OSRM/ORS, fallback/cache, cặp không có đường, gom cụm,
đóng cửa theo ngày, nghỉ trưa, giới hạn tuổi, thiếu thông tin và chặn Budget.
Chạy thử dữ liệu Đà Nẵng đã cho đủ 3 ngày; trong môi trường kiểm tra các API
không trả được ma trận nên thời gian của lần chạy đó là ước tính haversine.
