# SQLite — TravelBook Studio

Database mặc định: `backend/data/travelbook.db`, lấy từ `get_settings().database_path`.
Gọi `init()` khi khởi động; gọi lại không xóa dữ liệu.

| Bảng | Nội dung |
| --- | --- |
| projects | Yêu cầu, trạng thái, tiến độ, TripRequest, State, PDF và thời điểm tạo/cập nhật |
| tasks | Agent phụ trách, task/subtask, dependency, trạng thái và thời gian dự kiến |
| events | Nhật ký theo dự án/agent, loại sự kiện, thông điệp và payload JSON |
| artifacts | Metadata sản phẩm tương thích Artifact, gồm source và confidence |

```python
from backend.app.config import get_settings
from backend.app.db import Database

db = Database(get_settings().database_path)
db.init()
project = db.create_project("trip-001", "Đà Nẵng 3 ngày", "Một người lớn, ngân sách 5 triệu")
db.add_event("trip-001", "pm", "agent_started", "PM bắt đầu phân tích", {"stage": "pm"})
saved = Database(db.path).get_project("trip-001")
print(saved["title"])
print(db.get_events("trip-001"))
```

Các hàm:

- `create_project(id, title, prompt, trip_request=None, state=None)`: tạo dự án và
  sự kiện `project_created` trong cùng giao dịch. Hai tham số tùy chọn phải truyền bằng tên.
  ID trùng gây IntegrityError, không ghi đè dự án cũ.
- `get_project(id)`: trả dict hoặc None; `state` và `trip_request` đã giải mã,
  `state_json` là chuỗi JSON phục vụ lưu workflow.
- `list_projects(limit=100, offset=0)`: mới nhất trước.
- `update_project(id, **fields)`: chỉ cho phép trường được khai báo; trả bool.
- `add_event(project_id, agent, event_type, message, payload=None)`: trả ID sự kiện.
- `get_events(project_id, after_id=0, limit=1000)`: thứ tự tăng dần để theo dõi sự kiện mới.
- `replace_tasks(project_id, tasks)`: thay kế hoạch trong một giao dịch; kiểm tra
  dependency, vòng lặp, parent/subtask. ID task phải duy nhất trên toàn database.
- `get_tasks(project_id)`: trả task theo order_index; depends_on đã giải mã.
- `set_task_status(project_id, task_id, status)`: cập nhật một task theo ID.
- `add_artifact(artifact)`: nhận Artifact hoặc dict hợp schema; trả ID chuỗi.
- `get_artifact(project_id, artifact_id)` và `get_artifacts(project_id)`: đọc metadata.

Trạng thái task: todo (công việc mới), ready, in_progress, testing,
awaiting_approval, blocked, done. Chính sách chuyển trạng thái thuộc workflow.
Agent của task là một trong 10 vai trò TravelBook; event có thể ghi agent `system`.
Artifact chỉ được đăng ký metadata; hàm này không tạo hoặc kiểm tra file sản phẩm.

Các đối tượng Pydantic lồng trong JSON được chuyển về dữ liệu JSON, giữ source/confidence.
Computed fields như Budget.total được loại khỏi dữ liệu đầu vào để model đọc lại được.
TripRequest và Artifact được xác thực khi ghi; State/payload tùy biến cần được
agent/workflow xác thực bằng schema trước khi gọi database.

Kết nối bật foreign_keys, busy_timeout và dùng WAL. Mỗi giao dịch tự đóng kết nối,
commit khi thành công và rollback khi lỗi. init tạo parent directory nếu thiếu.
Không dùng `:memory:` vì lớp này mở kết nối riêng cho từng giao dịch.

Tạo và đọc lại một dự án demo thật (mỗi lần chạy tạo một ID mới):

```powershell
python scripts/check_database.py
python -m unittest discover -s tests -p test_db.py -v
```
