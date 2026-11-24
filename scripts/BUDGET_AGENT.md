# Budget Agent: cộng chi phí bằng code

```powershell
python scripts/check_budget.py
python scripts/check_budget.py --input-budget path/to/costs.json --itinerary path/to/itinerary.json --trip-request path/to/trip.json
```

Không gọi LLM hoặc mạng. CLI mặc định lấy các khoản từ JSON mẫu Đà Nẵng; khi
chạy chuyến khác cần truyền TripRequest thật để giới hạn lấy từ yêu cầu người
dùng. CLI tạo dự án/task riêng và chỉ seed các task trước đó là demo done.

## Đầu vào và giá

BudgetAgent.calculate(trip_request, items, itinerary=...) nhận list[BudgetItem].
amount là tổng của cả dòng cho cả nhóm, theo tiền tệ của TripRequest. Không
tự nhân lại theo travelers, không quy đổi tiền tệ hoặc tự bịa giá cho mục còn
thiếu. Muốn tính đơn giá × số người/đêm/chuyến, cần chuẩn bị dòng tổng với nguồn
phép tính rõ ràng trước khi truyền vào.

price_kind=sourced/estimated tách từng dòng. Giá có nguồn do caller cung cấp
chưa được agent kiểm chứng lại với nhà cung cấp. Nếu amount.source ghi rõ
fixture/mock/LLM/estimate/assumption/placeholder hoặc confidence=0 thì nhãn
sourced được hạ thành estimated, giữ nguyên amount, source và confidence gốc,
đồng thời ghi cảnh báo. Dòng đã là estimated không bị nâng lên sourced.

Agent không tạo dự toán tự động từ giá vé thiếu đơn vị, không coi giá vé của
Place là giá cho cả nhóm khi chưa biết cách tính. Danh sách trống cho tổng 0
nhưng needs_attention, không mặc định chuyến đi miễn phí/đã đủ dự toán.

## budget.json

| Trường | Ý nghĩa |
| --- | --- |
| currency, limit, items | Đầu vào chuẩn hóa; limit lấy từ TripRequest.budget |
| sourced_total | Tổng các dòng price_kind=sourced |
| estimated_total | Tổng các dòng price_kind=estimated |
| total | Tổng tất cả dòng, dùng Decimal |
| variance | total - limit; dương là vượt, âm là còn dư |
| remaining | limit - total; âm là thiếu ngân sách |
| within_budget | total <= limit |

Các trường tổng/so sánh đều là Fact với value/source/confidence. Tiền xuất ra
chuỗi thập phân để giữ độ chính xác; confidence của tổng là mức thấp nhất trong
các amount tham gia, của độ lệch tính thêm confidence của limit. Tổng nhóm rỗng
là 0 theo phép cộng, không phải bằng chứng đã thu thập đủ giá.

Budget schema vẫn nhận file cũ chỉ có currency/limit/items. Khi file mới có các
computed fields, validator tính lại từ các dòng rồi đối chiếu cả value/evidence;
tổng, độ lệch hoặc confidence bị sửa sẽ bị từ chối. Không âm thầm tin tổng nhập
vào. Nhờ vậy Map Agent, Content Editor, Reviewer và Layout dùng chung schema
có thể đọc budget.json mới mà không cần bỏ các trường tổng trước.
Schema JSON cho file đầu ra có thể lấy bằng Budget.model_json_schema(mode="serialization").
Khi sửa items thủ công trong file đã có tổng, cần tính lại các trường dẫn xuất
hoặc bỏ chúng khỏi đầu vào; tổng cũ không khớp sẽ bị từ chối.

## State, DB và điều phối

BudgetAgent.run(State) yêu cầu task Planner đã done và itinerary đủ ngày. Nhận
State.cost_items nếu có; nếu không, lấy items từ State.budget được cung cấp.
Budget có tiền tệ khác yêu cầu bị từ chối, không đổi tỷ giá. Không có cả hai
nguồn khoản chi thì xuất tổng danh sách rỗng, ghi thiếu dữ liệu và chặn bước sau.

Output riêng tại backend/data/artifacts/<project_id>/budget/<run_id>/:

- budget.json có đầy đủ các trường tổng và độ lệch.
- budget_report.json: trạng thái, cảnh báo giá và quy ước dấu variance.
- budget_validation.json tại thư mục dự án do CLI xuất.

Nếu có khoản chi và tổng trong ngân sách: task Budget done, Content Editor ready.
Vượt ngân sách hoặc không có khoản chi: dự án needs_attention, task Budget và
Content Editor blocked để sửa đầu vào. Không tự sửa ngân sách người dùng để làm
dự toán trở thành hợp lệ. production_approved=false, kể cả dùng giá có nguồn.
workflow.py fixture cũ không tự chuyển sang Budget Agent thật; module này có
thể nối sau Planner trong workflow thật.

Đã chạy JSON mẫu: tổng 3.700.000 VND, giá có nguồn 0, ước tính 3.700.000,
giới hạn 5.000.000, variance=-1.300.000, remaining=1.300.000, within_budget=true.
Đây là dự toán demo, không phải giá thị trường đã xác minh.

Kiểm thử tập trung cho Budget, schema và Reviewer không gọi API ngoài. Không
chạy lại toàn bộ suite đã bị auto-review chặn ở bước trước vì telemetry chưa
xác định; không dùng đường thực thi khác để bỏ qua chặn đó.
Kết quả: 11 test Budget, 8 test schema và 18 test Reviewer đều đạt.
