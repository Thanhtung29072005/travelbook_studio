# Researcher: địa điểm và thời tiết từ API công khai

```powershell
python scripts/check_researcher.py --destination "Đà Nẵng"
python scripts/check_researcher.py --destination "Đà Nẵng" --days 3 --start-date 2026-11-23
python scripts/check_researcher.py --trip-request path/to/trip_request.json
python scripts/check_researcher.py --mock --start-date 2026-11-23
```

Ngày đi cần nằm trong cửa sổ dự báo tại thời điểm chạy. Không truyền ngày sẽ không gọi dự báo.
--trip-request nhận đúng JSON đầu ra của PM. Nếu không truyền, CLI dùng brief demo có ghi nguồn
input:researcher-cli-demo. --mock dùng phản hồi HTTP giả lập, không phải dữ liệu du lịch thật.
CLI trả exit code 2 nếu không có địa điểm để xử lý tiếp, kể cả khi báo cáo đã được lưu.

## Nguồn và phạm vi

- Nominatim: geocode điểm đến theo query người dùng, lấy ứng viên hợp lệ đầu tiên.
  Tên mơ hồ vẫn cần đối chiếu; không dùng trường importance làm xác suất chính xác.
- Overpass: tìm attraction/museum/viewpoint/gallery, bãi biển và công viên quanh tọa độ,
  mặc định bán kính 15 km. Query giới hạn 60 phần tử; trả tối đa 12 địa điểm hợp lệ,
  có tên và tọa độ. Sở thích biển/bảo tàng được ưu tiên khi sắp xếp.
- Node dùng lat/lon; way/relation dùng center của bounding box, không phải lối vào địa điểm.
  Query dùng out body center để giữ tọa độ của node và center của way/relation.
- Wikidata: chỉ truy cập QID có sẵn từ tag wikidata trên OSM; không đoán entity theo tên.
  Mặc định enrich tối đa 4 entity, lấy mô tả vi/en và không ghi đè tọa độ OSM.
- Giờ mở cửa giữ nguyên tag opening_hours, chưa diễn giải thành lịch mở cửa thực tế.
  Chỉ nhận charge có số và mã tiền tệ rõ ràng. Giá thiếu/mơ hồ giữ null, không dùng LLM đoán.
  fee=no cũng không tự chuyển thành giá 0 với một tiền tệ giả định.
- Open-Meteo: nhiệt độ min/max °C, xác suất mưa %, tổng lượng giáng thủy mm,
  theo ngày địa phương từ timezone=auto. Không có ngày, ngoài horizon 16 ngày,
  lỗi API, đơn vị sai hoặc giá trị null đều không được thay bằng số bịa.
- Báo cáo thời tiết giữ ngày thiếu và dữ liệu lưới. Grid có thể lệch vài km so với tọa độ yêu cầu.
  Lịch dài chỉ truy vấn phần giao với horizon, giữ nguyên day_number so với ngày bắt đầu chuyến đi.

Mỗi dữ kiện được đưa vào Fact(value, source, confidence). Nguồn có URL và thời điểm truy xuất.
Confidence 0.6-0.8 là đánh giá bảo thủ của ứng dụng, không phải xác suất đã hiệu chỉnh.
Không tăng độ tin cậy chỉ vì cùng một dữ kiện được đọc lại từ cache.

## Kết nối và cache

PublicAPIClient dùng HTTPX, User-Agent nhận diện ứng dụng và SQLite cache riêng.
Nominatim được giới hạn tối thiểu 1 giây giữa các request, chia sẻ qua file cache
cho các process cùng dùng file đó. Đổi DATA_USER_AGENT thành thông tin nhận diện/liên hệ
của ứng dụng khi triển khai. Không dùng tính năng này để bulk geocode hoặc autocomplete.

Endpoint, timeout, cache path, bán kính và số kết quả có cấu hình trong .env.example.
Không thay đổi .env hiện có. Cache địa điểm mặc định 7 ngày, thời tiết 30 phút.
Không retry dồn dập khi public API báo lỗi/429. Header và input không chứa API key của 9Router.

Tham khảo tài liệu gốc:

- https://operations.osmfoundation.org/policies/nominatim/
- https://nominatim.org/release-docs/latest/api/Search/
- https://wiki.openstreetmap.org/wiki/Overpass_API/Overpass_QL
- https://www.wikidata.org/wiki/Wikidata:Data_access
- https://open-meteo.com/en/docs

## Agent và sản phẩm

ResearcherAgent là agent công cụ: nhận TripRequest từ PM, gọi các dịch vụ dữ liệu,
không dùng LLM để bịa địa điểm, giá vé hoặc dự báo. research(request) trả ResearchResult;
run(state) cập nhật DB, task, State và artifact.

Sản phẩm: researcher/<run_id>/research.json (list[Place]), weather.json (WeatherReport),
research_report.json (kết quả, notes, warnings, điểm đến). CLI ghi researcher_validation.json.
WeatherDay dùng chung với charts.WeatherPoint, có thể truyền report.daily cho forecasts khi vẽ.

Có địa điểm hợp lệ: Researcher done, Planner ready. Thời tiết có thể thiếu và vẫn giữ warning.
Không có tọa độ/địa điểm: project needs_attention, Researcher blocked, không giao Planner.
Wikidata lỗi không làm mất dữ liệu OSM đã nhận được. Không có fallback về fixture du lịch.

Đồ thị chạy mẫu vẫn đọc JSON như trước; các agent thật hiện có lệnh kiểm tra riêng.
Live test trong môi trường triển khai hiện gặp ConnectError với API bên ngoài;
HTTP giả lập kiểm tra schema, cache, nguồn, thiếu dữ liệu, lỗi upstream và chuyển task.

```powershell
python -m unittest discover -s tests -p test_researcher.py -v
```
