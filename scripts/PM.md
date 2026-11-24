# PM Agent thật: yêu cầu tự do -> TripRequest -> kế hoạch và task

PMAgent ở backend/app/agents/pm.py gọi NineRouterClient.chat_structured bằng LangChain.
Không có parser câu mẫu để thay LLM khi chạy thật. LLM trả TripDraft có bằng chứng;
backend kiểm tra bằng chứng, chuẩn hóa thành TripRequest, rồi tạo kế hoạch/task bằng code.

## Chạy với 9Router thật

Sau khi cài dependency, cấu hình .env với URL, API key và chuỗi model hợp lệ:

```powershell
python scripts/check_pm.py --prompt "Tôi muốn đi Đà Nẵng 3 ngày với 2 người lớn, tổng ngân sách 5 triệu đồng, thích biển và ăn hải sản."
```

Lệnh này gọi API thật và có thể tính token. Kết quả được lưu vào SQLite và
backend/data/artifacts/<project_id>/pm/<run_id>/plan.json, trip_request.json (nếu đủ dữ liệu).
pm_validation.json ghi rõ LIVE hoặc MOCK và các đường dẫn sản phẩm.

```powershell
python scripts/check_pm.py --mock
```

--mock chỉ minh họa câu mặc định bằng HTTP giả lập; không phải bằng chứng LLM đã hiểu câu nhập.
Kiểm thử tự động cũng giả lập phản hồi HTTP, nhưng chạy router, Pydantic, PM, SQLite và cache thật.

## API

```python
from backend.app.agents.pm import PMAgent
from backend.app.db import Database
from backend.app.services.router import NineRouterClient
from backend.app.config import get_settings

async def plan_trip():
    settings = get_settings()
    db = Database(settings.database_path)
    db.init()
    prompt = "Đà Nẵng 3 ngày, 2 người, tổng ngân sách 5 triệu đồng, thích biển."
    db.create_project("my-trip-01", "Chuyến đi của tôi", prompt)
    async with NineRouterClient(settings) as router:
        pm = PMAgent(db, router, settings.artifacts_dir)
        update = await pm.run({"project_id": "my-trip-01", "user_prompt": prompt})
        return update
```

PMAgent.normalize(prompt, project_id="draft", reference_date=None, models=None) chỉ chuẩn hóa
và trả PMResult, không tạo dự án/task trong DB. PMAgent.run(state) lưu kết quả vào dự án đã có.
Kết quả run là cập nhật từng phần cho State; artifacts trả về chỉ gồm sản phẩm mới.

## Trạng thái và dữ liệu

- ready: có TripRequest hợp schema, plan và 11 task. Task PM hoàn tất, Researcher ready;
  các task còn lại chờ dependency. Dự án queued tại stage researcher, chưa tự chạy agent kế tiếp.
- needs_clarification: trip_request=null, không giao task; dự án awaiting_input với câu hỏi.
  Caller ghép câu gốc và câu trả lời bổ sung thành user_prompt mới khi gọi lại PM.
- Không tự mặc định số người, tuổi, điểm đến hoặc tiền tệ. '5 triệu' chưa rõ tiền tệ thì hỏi lại.
- '5 triệu đồng' được chuẩn hóa thành Decimal 5000000 và VND. Ngân sách mỗi người được
  nhân với tổng số người bằng Decimal, có source calculation riêng; không giao LLM tự cộng tổng.
- Tuổi biết chưa đủ toàn nhóm được giữ trong requirements, không gán tuổi giả cho người lớn.
- Ngày tương đối dùng reference_date theo UTC+07:00; ngày mơ hồ/mâu thuẫn phải hỏi lại.
- Mọi dữ kiện cuối có source là hash câu nhập + evidence nguyên văn và confidence.
  Code xác nhận evidence xuất hiện trong câu nhập; không thể chứng minh mọi suy luận ngữ nghĩa
  của model đều đúng chỉ bằng kiểm tra chuỗi trích dẫn.
- Cache/router fallback được dùng lại. Các sự kiện model_call/model_fallback, plan_ready,
  clarification_requested hoặc agent_failed được ghi vào events.
- File PM lưu theo run_id riêng để tránh ghi đè sản phẩm của lần lập kế hoạch trước.

task_plan.py là nguồn dependency chung cho PM thật và workflow fixture, nên PM không thể
tự thêm/bớt vai trò hoặc đổi nhánh song song theo phản hồi model. PM chỉ lập kế hoạch,
không tự viết lịch trình, dự toán chi tiết, tạo ảnh hoặc PDF thay các agent chuyên môn.

scripts/run_sample_workflow.py vẫn chạy toàn bộ node bằng JSON mẫu. PM thật hiện được
kiểm tra riêng bằng check_pm.py; chưa thay node PM của đồ thị fixture.

## Bằng chứng chạy tại thời điểm triển khai

Câu mặc định đã được gửi qua 9Router tới cx/gpt-5.6-sol, trả ready:
Đà Nẵng, 3 ngày, 2 người, tổng ngân sách 5000000 VND, sở thích biển/hải sản,
TripRequest hợp schema và 11 task được ghi vào SQLite.
Lượt đầu cache_hit=false, usage được provider báo 2261 prompt tokens và 306 completion tokens.
Lượt chạy lại cùng câu nhập trả cache_hit=true và cùng TripRequest hợp schema.
Đây là một lần kiểm tra live cụ thể, không phải bảo đảm mọi câu nhập hoặc mọi model luôn trích xuất đúng.

```powershell
python -m unittest discover -s tests -p test_pm.py -v
```
