# TravelBook Studio

Thông báo từng agent và gửi PDF qua Telegram: [scripts/TELEGRAM.md](scripts/TELEGRAM.md).

Nguồn địa điểm dự phòng Wikidata và kiểm tra thực tế Nha Trang: [scripts/INDEPENDENT_PLACE_FALLBACK.md](scripts/INDEPENDENT_PLACE_FALLBACK.md).

Luồng UI mặc định đã nối agent thật. Sau cập nhật hãy khởi động lại backend và tạo dự án mới; xem [scripts/LIVE_WORKFLOW.md](scripts/LIVE_WORKFLOW.md) về dữ liệu, cấu hình và giới hạn kiểm thử.

Agent/model management update: [scripts/AGENT_MODEL_MANAGEMENT.md](scripts/AGENT_MODEL_MANAGEMENT.md).

Luồng CLI offline đã chạy được từ JSON mẫu qua LangGraph đến PDF.
PM -> Researcher -> Planner -> Budget -> Content Editor -> ba vai trò hình ảnh song song
-> Reviewer -> Layout Editor -> PM xác nhận.

## Cài đặt lần đầu trên Windows PowerShell

Yêu cầu Python 3.11 trở lên.

    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e ".[dev]"

## Một lệnh chạy toàn bộ đồ thị

    .\.venv\Scripts\python.exe scripts/run_sample_workflow.py

Nếu môi trường Python đã có đủ dependency:

    python scripts/run_sample_workflow.py

Mỗi lần chạy tạo dự án mới trong SQLite và thư mục riêng dưới backend/data/artifacts.
Lệnh in đường dẫn PDF, số node/task/sự kiện và số trang.
JSON nguồn trong backend/data/artifacts/danang_3days không bị ghi đè.

Đây là demo fixture: mọi node đọc JSON mẫu, không gọi LLM/9Router hoặc API ngoài.
Review chỉ kiểm tra cấu trúc luồng và file; production_approved luôn false.
PDF mẫu có 11 trang. Bản đồ là sơ đồ tọa độ; thời tiết chưa có dự báo;
ảnh chưa xác minh không được nhúng. PM Agent thật đã có lệnh kiểm tra riêng;
Researcher Agent có dịch vụ API công khai và lệnh kiểm tra riêng;
các agent thực còn lại, vòng sửa, pause/resume, API và frontend chưa hoàn thiện.

## Các phần đã có

- Schema Pydantic có source/confidence và State dùng chung.
- Router 9Router qua LangChain, fallback và cache SQLite.
- SQLite projects/tasks/events/artifacts.
- Style lock, Pillow resize/crop/placeholder.
- Bản đồ và biểu đồ PNG từ dữ liệu.
- ReportLab xuất PDF có nguồn ảnh, số trang và manifest.
- StateGraph fixture với Send API, fan-in, sự kiện và snapshot DB.
- PM Agent gọi LLM để chuẩn hóa câu nhập thành TripRequest và lập kế hoạch/task.
- Researcher lấy dữ liệu Nominatim/Overpass/Wikidata và Open-Meteo, giữ nguồn/confidence.

Chạy PM thật (cần 9Router đang hoạt động và .env hợp lệ):

    python scripts/check_pm.py --prompt "Đà Nẵng 3 ngày, 2 người, tổng ngân sách 5 triệu đồng, thích biển."

Xem scripts/PM.md để biết chế độ cần làm rõ và cách giữ nguồn/confidence.

Chi tiết tại scripts/WORKFLOW.md; tài liệu từng module nằm trong scripts/.
Không đưa API key thật vào mã nguồn hoặc tài liệu.

## Kiểm thử

    python -m unittest discover -s tests -v

## Planner Agent

Planner gom cum dia diem, xep theo gio mo cua va do tuoi, dung OSRM/ORS hoac haversine du phong.
Moi diem co gio den/roi va thoi gian di chuyen kem source/confidence.

    python scripts/check_planner.py --offline

Chi tiet va gioi han: [scripts/PLANNER.md](scripts/PLANNER.md).
Workflow hien tai van chay fixture; agent that co CLI kiem tra rieng.

## Content Editor Agent

Bien tap loi mo dau tung ngay, meo, mon an va luu y van hoa qua 9Router.
content.json giu schema danh sach Fact; content_sections.json tach cac nhom noi dung.

    python scripts/check_content_editor.py --mock

Chay LLM that: bo --mock. Chi tiet: [scripts/CONTENT_EDITOR.md](scripts/CONTENT_EDITOR.md).

## Image Curator Agent

Tim anh Wikimedia/Unsplash/Pexels, chon toi da mot anh moi dia diem va luu tac gia, license, nguon va ghi cong.

    python scripts/check_image_curator.py --mock

Bo --mock de goi API that. Can UNSPLASH_ACCESS_KEY / PEXELS_API_KEY cho hai nguon nay.
Chi tiet: [scripts/PHOTOS.md](scripts/PHOTOS.md). Anh remote chua tu dong duoc nhung vao PDF.

## Illustrator Agent

Tao bia, anh mo dau moi ngay, 4 icon va 2 nen cung style lock; loi thi dung nen du phong dung kich thuoc.

    python scripts/check_illustrator.py

Them --offline de dung du phong khong goi model, --mock de kiem tra HTTP gia lap.
Chi tiet: [scripts/ILLUSTRATOR.md](scripts/ILLUSTRATOR.md).

## Map Agent tu file du an

Doc itinerary.json cua Planner va JSON dia diem/ngan sach/thoi tiet duoc cung cap.
Khong tu quay ve fixture khi thieu file; luu hash va ban chup dau vao cho moi luot ve.

    python scripts/check_map_agent.py --itinerary path/to/itinerary.json --places path/to/research.json --budget path/to/budget.json

Them --weather path/to/weather.json neu co WeatherReport dung ngay.
Chi tiet: [scripts/MAP_AGENT.md](scripts/MAP_AGENT.md).

## Visual Team: Send va fan-in

Ba agent that chay tuan tu de kiem tra, sau do chay song song bang Send.
Fan-in gop photos/illustrations/maps/charts va artifacts, chi mo Reviewer khi du ket qua.

    python scripts/check_visual_team.py --mock

Mock HTTP nha cung cap; StateGraph, thread, agent, DB va file deu chay that.
Chi tiet API/live mode: [scripts/VISUAL_TEAM.md](scripts/VISUAL_TEAM.md).

## Reviewer Agent: code rules

Kiem tra gio mo cua/ngay gio tham quan, tong chi phi, quang duong moi ngay va metadata nguon/license anh.

    python scripts/check_reviewer.py

JSON mau can REVISE la ket qua dung. PASS mo task Layout, REVISE chan Layout va ghi agent can sua.
Chi tiet: [scripts/REVIEWER.md](scripts/REVIEWER.md).

## Budget Agent: tong va do lech

Cong cac khoan chi bang Decimal, tach sourced_total/estimated_total, xuat total/variance/remaining/within_budget.

    python scripts/check_budget.py

Chi tiet: [scripts/BUDGET_AGENT.md](scripts/BUDGET_AGENT.md).

## System prompts va quyet dinh LLM

Researcher, Planner, Image Curator va Reviewer da co SYSTEM prompt va quyet dinh JSON qua 9Router.
Code van kiem tra ID, gio mo cua, do tuoi, metadata license va cac loi Reviewer.

    python scripts/check_agent_reasoning.py --mock

Bo --mock de thu LLM that (du lieu API trong bai smoke van la mock).
Bat/tat bang AGENT_REASONING_ENABLED; AGENT_REASONING_REQUIRED=true dung khi quyet dinh LLM that bai.
Chi tiet va gioi han tich hop: [scripts/AGENT_REASONING.md](scripts/AGENT_REASONING.md).

## Reviewer: anh, style va do tuoi bang LLM

Gui pixels anh cho model vision qua 9Router, kiem tra dung dia diem, style minh hoa va phu hop do tuoi.
ReviewIssue co classification critical/warning, rule_code, evidence_ids va target_agent.
Thieu bang chung tra unknown va chan Layout; canh bao nhe khong chan khi cac kiem tra khac dat.

    python scripts/check_visual_review.py --mock

Cau hinh NINE_ROUTER_VISION_MODELS de chay that voi --state path/to/state.json.
Chi tiet: [scripts/VISUAL_REVIEW.md](scripts/VISUAL_REVIEW.md).

## Conditional edge va gioi han sua loi

Reviewer dinh tuyen loi theo bang rule/category ve dung agent; sua lai cac buoc phu thuoc roi review lai.
MAX_REVISIONS gioi han so vong sua. Het vong thi giu REVISE, ghi canh bao vao DB/JSON/PDF va xuat ban nhap.

    python scripts/check_revision_routing.py --agent image_curator --max-revisions 2

Loi co y se quay lai Image Curator 2 lan, review 3 lan, sau do di tiep Layout.
Chi tiet: [scripts/REVISION_ROUTING.md](scripts/REVISION_ROUTING.md).

## Layout Editor hoan chinh

Tao PDF tu State that: muc luc co so trang, tung ngay gom anh/minh hoa, lich, ban do va meo;
ngan sach/thoi tiet, trang nguon anh, va ghi chu Nen kiem tra lai cho du kien confidence thap.
Co audit toa do chu/anh, chan xuat neu phat hien chong len nhau.

    python scripts/check_layout_editor.py

Them --state path/to/state.json de doc du lieu du an, hoac --illustration-dir path/to/illustrator/output de xem demo voi minh hoa san co.
Chi tiet: [scripts/LAYOUT_EDITOR.md](scripts/LAYOUT_EDITOR.md).

## FastAPI va Swagger

Tao, liet ke, xem chi tiet va xoa du an; doc su kien, artifact va tai file qua API.

    python -m pip install -e .
    python -m backend.app.main

Mo http://127.0.0.1:8000/docs de thu endpoint. Tao du an luu brief, chua tu chay workflow.
Co the tao artifact demo de thu tai file bang `python scripts/check_api.py --seed`.
Chi tiet: [scripts/API.md](scripts/API.md).

## Frontend workspace

Sau tab: Tong quan, List, Lich, Giao tiep, Agent, San pham; co trang model 9Router.
Quan ly ho so agent, duyet tung giai doan, sua JSON hop schema va giao lai agent tu su kien.
Dieu huong URL, cursor su kien va phan trang san pham/du an da duoc bo sung.
Chi tiet: [scripts/WORKSPACE_UI.md](scripts/WORKSPACE_UI.md).

## Pause/resume bang checkpoint SQLite

Yeu cau tam dung se cho node/nhom Send hien tai hoan tat va ghi checkpoint truoc khi chuyen sang paused.
Tiep tuc tu checkpoint LangGraph, ke ca khi tao lai manager hoac mo lai backend.

    python scripts/check_pause_resume.py

Them --pause-only, sau do --resume PROJECT_ID trong tien trinh khac de kiem tra luu ben vung.
Chi tiet: [scripts/PAUSE_RESUME.md](scripts/PAUSE_RESUME.md).

## Duyet thu cong bang LangGraph interrupt

Lich trinh, anh va PDF dung `interrupt`; hai nut Duyet / Yeu cau sua gui `Command(resume=...)` tren checkpoint SQLite.
Sua quay ve dung agent, luu dau ra moi va hoi duyet lai. Khong can API key cho demo JSON mau.

    python scripts/check_manual_approvals.py

Chi tiet va kiem tra hai tien trinh: [scripts/MANUAL_APPROVALS.md](scripts/MANUAL_APPROVALS.md).

## Supervisor lai: code + LLM

Bat `SUPERVISOR_ENABLED=true` de code loc agent du dieu kien, LLM chon batch bang structured output, roi dispatch bang Send.
Giu dependency, checkpoint, duyet thu cong va gioi han sua loi; fallback chi chon trong tap hop le.

    python scripts/check_supervisor.py

Them `--serial` de thu chon tung visual, `--live` de supervisor goi 9Router that.
Chi tiet: [scripts/HYBRID_SUPERVISOR.md](scripts/HYBRID_SUPERVISOR.md).
