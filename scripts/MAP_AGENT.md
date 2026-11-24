# Map Agent: đọc file dự án

```powershell
python scripts/check_map_agent.py `
  --itinerary path/to/planner/itinerary.json `
  --places path/to/researcher/research.json `
  --budget path/to/budget/budget.json `
  --weather path/to/researcher/weather.json
```

Ba file itinerary/places/budget bắt buộc. WeatherReport JSON là tùy chọn;
--trip-request path/to/trip.json cho phép kiểm tra thêm số ngày, ngày đi,
giới hạn ngân sách và tiền tệ của cùng chuyến. --research là alias của --places.
Lệnh không mặc định tới danang_3days, không tự tải dữ liệu hay gọi LLM.
CLI tạo dự án/task kiểm tra riêng, chỉ đánh dấu các bước trước ở trạng thái demo
done để chạy độc lập; không thực thi lại PM/Researcher/Planner/Budget/Content Editor.

## Đầu vào

- itinerary.json: list[DayPlan], thứ tự ngày 1..N liên tiếp, 1..31 ngày. Các giờ
  và thời gian di chuyển phải hợp schema. Đọc bytes file mới tại mỗi lần chạy;
  không dùng State.itinerary cũ để ghi đè dữ liệu từ file được chỉ định.
- research.json: list[Place], ID không trùng, mọi stop phải có tọa độ/tên tương ứng.
- budget.json: Budget theo hợp đồng JSON hiện có; có thể dùng bản chỉ có các
  khoản chi hoặc bản Budget Agent xuất kèm tổng/độ lệch. Các computed fields
  được schema tính lại và đối chiếu, không lấy tổng nhập vào làm kết quả.
  Tổng tính lại bằng Decimal; day_number của khoản chi phải thuộc lịch trình.
- weather.json: WeatherReport của Researcher/Open-Meteo, không phải list văn bản.
  Chỉ ghép số liệu khi date của WeatherDay và DayPlan cùng có và khớp; sai ngày
  làm task thất bại. Thiếu ngày cụ thể thì bỏ dự báo đó và ghi cảnh báo. Ngày thiếu
  dự báo giữ trống, không nội suy hoặc tự điền nhiệt độ/xác suất mưa.

File tối đa 5 MB, được đọc một lần để giữ dữ liệu nhất quán trong cả lượt vẽ.
Kiểm toàn bộ bundle trước khi tạo PNG. File thiếu/sai schema, ID không tồn tại,
ngày thời tiết lệch hoặc ngân sách sai phạm vi đều không được thay bằng JSON mẫu.

## Kết quả

Mỗi lượt ghi vào backend/data/artifacts/<project_id>/map_agent/<run_id>/:

- maps/day1.png .. dayN.png: 1200 × 800, tọa độ và thứ tự lấy nguyên từ file.
- charts/budget.png: 1200 × 800, tách khoản có nguồn với khoản ước tính.
- charts/weather.png: 1200 × 800, dự báo số hoặc thông báo thiếu dữ liệu.
- charts/timeline.png: rộng 1400, cao theo số điểm (900..6000), dùng giờ và
  thời gian di chuyển thực sự trong itinerary.json.
- inputs/*.json: bản chụp nguyên bytes đầu vào; giữ được dữ liệu của lượt vẽ
  kể cả khi file gốc được sửa sau đó.
- maps_charts.json: MapResult, chứa Artifact map/chart, đường dẫn gốc/bản chụp,
  SHA256, source/confidence, contains_demo_input, has_numeric_weather, cảnh báo.
- map_agent_validation.json ở thư mục dự án: kết quả kiểm tra CLI và dữ liệu PNG.

Metadata PNG giữ Source, Confidence, Notes, Data của renderer; thêm Inputs là
hash từng file liên quan. Ngân sách/thời tiết không bị lấy từ file cố định khác
với file được truyền vào. Khi thay JSON và chạy lại sẽ có thư mục/lượt vẽ mới.

Bản đồ hiện là sơ đồ tọa độ với đoạn thẳng thể hiện thứ tự, chưa dùng map tiles
hay hình học tuyến đường OSRM/ORS. Thời gian di chuyển giữ nguyên từ Planner;
thời gian ước tính vẫn mang nguồn/confidence ước tính. Đây là vẽ dữ liệu dự án,
không tự xác minh lại tọa độ, giá hoặc dự báo với nhà cung cấp.

## Tích hợp State/DB

```python
agent = MapAgent(db, settings.artifacts_dir)
update = await agent.run(state, inputs={
    "itinerary": planner_itinerary_path,
    "places": researcher_places_path,
    "budget": budget_output_path,
    "weather": weather_output_path,  # Có thể bỏ.
})
```

Thứ tự chọn file: inputs truyền trực tiếp -> State.map_input_paths -> Artifact
upstream cùng project (planner/itinerary.json, researcher/research.json,
budget/budget.json, researcher/weather.json). Không có file bắt buộc thì lỗi.
MapInputFiles cho phép Path hoặc chuỗi. Agent nhận các tên file tùy ý khi được
truyền trực tiếp; chỉ cơ chế dò Artifact dùng tên output chuẩn theo vai trò.

Task Content Editor phụ thuộc phải done. run ghi events/artifacts vào SQLite,
trả State.maps/charts/map_warnings/map_input_paths. Chỉ mở Reviewer khi cả ba
nhánh Image Curator/Illustrator/Map Agent đã done; nhánh khác blocked thì giữ
needs_attention. production_approved=false. workflow.py hiện vẫn là đồ thị
fixture, chưa tự chuyển toàn bộ node sang agent thật.

## Phạm vi “dữ liệu thật” và kiểm chứng

Map Agent đọc file của lượt chạy dự án thay vì đọc ngầm fixture. Nguồn có marker
fixture/mock được báo contains_demo_input=true; nguồn do người dùng nhập không
tự được coi là đã xác minh chỉ vì file không có marker này.

Đã chạy với itinerary.json do Planner thực thi tạo ra ở
planner-live-a0b6b14e7e85/planner/c76321366d384a24. Tọa độ và ngân sách đi kèm
lần kiểm tra vẫn là dữ liệu Đà Nẵng mẫu chưa xác minh, nên báo cáo giữ cảnh báo
demo; thiếu weather.json thì biểu đồ không có nhiệt độ/mưa bịa thêm. Budget Agent
hiện còn là stub, cần truyền Budget JSON được cung cấp cho bước này.

```powershell
python -m unittest discover -s tests -v
```

Kiểm thử thay thứ tự điểm/số tiền trong JSON, đọc lại khi file thay đổi, dự báo số
đúng ngày, thiếu dự báo, ngày lệch, ID sai, hash/bản chụp và barrier Reviewer.
