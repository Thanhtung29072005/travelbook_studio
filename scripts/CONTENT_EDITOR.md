# Content Editor

```powershell
python scripts/check_content_editor.py --mock
python scripts/check_content_editor.py
python scripts/check_content_editor.py --input-dir path/to/json --trip-request path/to/trip.json
```

Cài dependency theo README. Lệnh không có --mock gọi LLM qua 9Router trong .env;
dùng router LangChain hiện có, structured output, model fallback và cache theo hash.
--mock dùng HTTP giả lập, không gọi LLM thật. CLI gọi biên tập lần hai với cùng
đầu vào và báo second_call_cache_hit để kiểm tra cache.

Thư mục đầu vào cần research.json (list[Place]), itinerary.json (list[DayPlan]),
budget.json (Budget). Nếu không truyền TripRequest, CLI dùng brief demo Đà Nẵng,
một người, số ngày và ngân sách từ JSON mẫu; không suy ra số người từ tổng tiền.
Muốn biên tập chuyến khác phải truyền --trip-request tương ứng.

## Đầu ra

Mỗi lần chạy lưu vào thư mục dự án riêng, không ghi đè JSON mẫu:

- content.json: list[Fact[Text]], giữ đúng hợp đồng schema cũ của LayoutService.
  Có lời mở đầu toàn chuyến, “Ngày N:”, “Mẹo:”, “Món ăn:”, “Văn hóa:”, “Lưu ý:”.
- content_sections.json: ContentDocument, gồm introduction, days (DayContent),
  tips, food, culture, warnings. Số ngày liên tiếp từ 1, không trùng, đầy đủ theo
  TripRequest; mọi văn bản có value/source/confidence.
- content_validation.json: kết quả kiểm schema, số ngày/mục, model và cache.

Schema Pydantic nằm trong backend/app/schemas.py. ContentDraft nội bộ nhận văn
bản, evidence_ids và cờ suggestion; mô hình không được tự gán nguồn/confidence.
Backend đối chiếu ID với các Fact đầu vào, gắn nguồn LLM + hash ngữ cảnh + nguồn
Fact tham chiếu. Câu thiếu bằng chứng phải là gợi ý, có nhãn “Gợi ý chưa xác minh”
và confidence tối đa 0.3. Diễn đạt từ dữ liệu có confidence tối đa 0.6 và không
vượt nguồn đầu vào. Không tự nâng dữ liệu fixture thành dữ kiện đã xác minh.

ID hợp lệ chỉ xác nhận nguồn tham chiếu tồn tại, không chứng minh câu do LLM
viết được nguồn hỗ trợ về ngữ nghĩa. Nội dung vẫn cần Reviewer kiểm duyệt;
production_approved luôn false. Agent không tìm web, nhà hàng, giá hay dữ liệu
ẩm thực/văn hóa mới; muốn nội dung có nguồn thực thì đưa Fact vào research.
Mặc định research được nhận từ State.research, không tái dùng content mẫu làm nguồn.

## Tích hợp

ContentEditorAgent.edit(trip_request, itinerary, places, budget=..., research=...)
trả ContentResult có document, model, cache_hit, usage. Phương thức run(State)
yêu cầu dự án và task Content Editor đã có, Budget phụ thuộc đã done và State
có budget. run ghi sự kiện DB, lưu/đăng ký artifact, cập nhật content dạng phẳng
và content_document dạng mục. Thành công mở task Image Curator, Illustrator,
Map Agent; lỗi chặn task, không phát hành artifact nội dung không hợp lệ.

CLI chỉ khởi tạo task phía trước dưới dạng dữ liệu demo, không thực thi các
agent PM/Researcher/Planner/Budget trong lệnh này. workflow.py vẫn là đồ thị
fixture; việc nối các agent thật vào một đồ thị sẽ thực hiện riêng.

```powershell
python -m unittest discover -s tests -v
```

Kiểm thử gồm schema/nguồn, đủ lời mở đầu theo ngày, gợi ý thiếu nguồn, từ chối
ID nguồn giả, cache/fallback, task/sự kiện/artifact và lỗi phụ thuộc.
