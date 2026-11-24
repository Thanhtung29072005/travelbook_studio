# PNG bản đồ và biểu đồ từ JSON

```powershell
python scripts/check_maps_charts.py
python -m unittest discover -s tests -p test_maps_charts.py -v
```

Script đọc research.json, itinerary.json và budget.json từ
`backend/data/artifacts/danang_3days/`, xác thực bằng schema hiện có và tạo:

| File | Kích thước |
| --- | --- |
| maps/day1.png | 1200 × 800 |
| maps/day2.png | 1200 × 800 |
| maps/day3.png | 1200 × 800 |
| charts/budget.png | 1200 × 800 |
| charts/weather.png | 1200 × 800 |
| charts/timeline.png | 1400 × 900 |

`maps_charts_validation.json` ghi kích thước, nguồn, confidence, ghi chú và dữ liệu vẽ.
Script mở lại từng PNG để xác nhận kích thước thực tế.

## API

```python
from backend.app.services.maps import render_day_map, render_day_maps
from backend.app.services.charts import (
    render_budget_chart, render_weather_chart, render_timeline_chart,
)

# days: list[DayPlan hoặc dict]; places: list[Place hoặc dict]; budget: Budget hoặc dict
paths = render_day_maps(days, places, "output/maps")
render_budget_chart(budget, "output/charts/budget.png")
render_weather_chart(days, "output/charts/weather.png")
render_timeline_chart(days, places, "output/charts/timeline.png")
```

Các hàm trả Path; có thể truyền `size=(width, height)` để đổi kích thước.
Matplotlib Agg render không cần GUI; Pillow bảo đảm kích thước PNG chính xác.
`plotting.py` là helper dùng chung, có khóa để tránh render đồng thời không an toàn.

## Phạm vi dữ liệu

- Bản đồ là sơ đồ tọa độ offline, đánh số điểm theo lịch trình và nối thẳng từng điểm.
  Không có nền đường phố/tile, không phải kết quả routing hoặc hướng dẫn lái xe.
  Tọa độ fixture được ghi rõ là demo chưa xác minh; cần routing service để vẽ tuyến đường thật.
- Ngân sách cộng bằng Decimal, tách giá có nguồn và giá ước tính theo màu.
  Với JSON mẫu: tổng 3.700.000 VND, giới hạn 5.000.000 VND.
- Timeline lấy đúng giờ đến, giờ rời đi và phút di chuyển trong JSON;
  không tự gán hoạt động vào các khoảng trống. Giờ là giờ địa phương của điểm đến.
- JSON mẫu chưa có ngày đi và dự báo thời tiết: weather.png hiển thị trạng thái thiếu dữ liệu,
  confidence 0, không tạo nhiệt độ hay xác suất mưa giả.
- Khi có dự báo số, truyền `forecasts=list[WeatherPoint hoặc dict]` cho render_weather_chart.
  Mỗi point gồm day_number, temperature_min/temperature_max (°C) và rain_probability (%).
  Các giá trị là Fact có value/source/confidence; ngày thiếu dữ liệu không được nội suy.

PNG chứa metadata `Source`, `Confidence`, `Notes`, `Data`.
Confidence là giá trị nhỏ nhất của các Fact liên quan; không tăng độ tin cậy chỉ vì đã vẽ được ảnh.
Metadata dùng JSON để giữ nguồn và các số liệu đầu vào cho việc đối chiếu.
