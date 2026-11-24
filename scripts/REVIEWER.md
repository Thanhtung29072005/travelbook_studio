# Reviewer: luật bằng code

```powershell
python scripts/check_reviewer.py
python scripts/check_reviewer.py --input-dir path/to/json --trip-request path/to/trip.json
python scripts/check_reviewer.py --itinerary path/to/itinerary.json --places path/to/research.json --budget path/to/budget.json --photos path/to/photos.json --trip-request path/to/trip.json
```

Không gọi LLM hoặc mạng. Mặc định kiểm tra JSON mẫu Đà Nẵng và trả REVISE;
đây là kết quả đúng vì mẫu còn thiếu dữ kiện/ảnh xác minh. Không truyền
--trip-request thì CLI dùng brief demo một người, ngân sách/số ngày từ mẫu.
Muốn xét chuyến thật cần truyền TripRequest thực tế để tránh lấy limit của
budget.json làm yêu cầu gốc.

Exit code 0 khi PASS, 2 khi REVISE. CLI tạo project riêng và đánh dấu các task
phụ thuộc là demo done để kiểm tra độc lập, không thực thi lại Visual Team.

## Các luật

| Nhóm | Kiểm tra | Agent nhận yêu cầu sửa |
| --- | --- | --- |
| Giờ mở cửa | Toàn bộ arrival..departure phải trong cùng một cửa sổ mở cửa; đúng thứ/ngày, không vượt giờ nghỉ trưa/giờ đóng cửa | Planner |
| Thiếu giờ/ngày | Giờ trống, confidence=0, biểu thức ngoài parser; thiếu ngày khi lịch đổi theo thứ | Researcher hoặc PM |
| Ngân sách | Cùng tiền tệ, Budget.limit không tự thay TripRequest.budget; cộng Decimal và so tổng với ngân sách yêu cầu | Budget |
| Dữ liệu chi phí | Không có khoản chi hoặc khoản chi thuộc ngày ngoài chuyến | Budget |
| Quãng đường/ngày | Cộng distance_km được khai báo, vượt ngưỡng hoặc thiếu chặng giữa hai điểm thì cần sửa/bổ sung | Planner |
| Ảnh | Một ảnh mỗi địa điểm; không trùng ID; đúng provider/image host/trang ảnh; tác giả, license, license URL, nguồn và confidence hợp lệ | Image Curator |
| Dữ liệu mẫu | Fixture/mock/placeholder hoặc nguồn ghi chưa xác minh không được qua như dữ kiện thật | Researcher / Image Curator |

Giờ xét tới giây, không cắt bỏ giây để ảnh hưởng ranh giới đóng cửa. Nếu ngày
của DayPlan thiếu nhưng TripRequest có start_date, lấy start_date + day_number-1.
Nếu cả hai cùng có mà không khớp thì báo lỗi. 24/7 hoặc khung giống nhau mọi ngày
có thể xét khi chưa biết ngày; lịch khác nhau theo thứ cần ngày cụ thể.
Parser dùng cùng services/opening_hours.py với Planner; biểu thức ngày lễ, qua
đêm, lịch mùa phức tạp chưa hỗ trợ sẽ là unknown, không tự mặc định đang mở cửa.

Ngân sách được tính lại từ amount, không tin tổng do LLM cung cấp. amount là
tổng của cả dòng cho cả nhóm theo schema hiện có, không phải đơn giá/người.
Reviewer chưa kiểm chứng báo giá hoặc bảo đảm dự toán đủ mọi khoản chi.

Quãng đường mặc định: driving 100, walking 15, cycling 50 km/ngày. Đây là chính
sách lập lịch có thể chỉnh, không phải giới hạn giao thông/sức khỏe:

```dotenv
REVIEWER_MAX_DAILY_DISTANCE_KM=80
```

Hoặc CLI --max-distance 80. Chỉ cộng các chặng đã khai báo. Chặng đầu None được
hiểu là ngoài phạm vi bắt đầu tại điểm đầu, không tự thêm chặng khách sạn/đi về.
Thiếu distance_km của các chặng tiếp theo không được thay bằng 0. Haversine vẫn
được cộng/so ngưỡng nhưng có issue minor DISTANCE_ESTIMATED; số ước tính không
biến thành khoảng cách đường bộ đã xác minh. Confidence=0 cần bổ sung dữ liệu.

## Phạm vi kiểm tra ảnh

Wikimedia dùng commons_license của attribution.py: kiểm tên giấy phép và URL
khớp trong tập CC BY/CC BY-SA/CC0/Public Domain Mark được adapter hỗ trợ.
Unsplash/Pexels yêu cầu đúng giấy phép nguồn, trang ảnh cụ thể và liên kết tác
giả; không chấp nhận chỉ đưa homepage. Ảnh Unsplash khai báo local_path bị chặn
vì renderer hiện không hỗ trợ điều kiện hotlink của API. File ảnh local khác
được Pillow kiểm tra định dạng/kích thước/tính đọc được.

Ảnh placeholder, metadata MOCK, nguồn generation/LLM, license trống/sai,
tác giả unknown, confidence=0 hoặc source host không tương ứng đều bị báo lỗi.
Giấy phép ngoài tập hỗ trợ cần bổ sung adapter/kiểm tra riêng; bị từ chối ở đây
không có nghĩa ảnh đó chắc chắn vi phạm bản quyền.

Đây là kiểm tra metadata do đầu vào cung cấp, không gọi lại API/đọc trang nguồn,
không chứng minh URL còn truy cập được, ảnh đúng cảnh hay tác giả sở hữu quyền.
PIL đọc được pixels cũng không chứng minh ảnh thật. Bản quyền/đồng ý người mẫu/
nhãn hiệu không được xác nhận chỉ bằng phép kiểm này; scope được lưu trong report.

## Report và task

ReviewerAgent.evaluate(State) là hàm luật, dùng không cần lifecycle dự án.
ReviewerAgent.run(State) yêu cầu dự án và task Reviewer, ba nhánh Visual Team
phụ thuộc đã done. Kết quả lưu dưới reviewer/<run_id>/:

- review.json: Review hợp schema; verdict PASS hoặc REVISE, summary, issues.
- review_checks.json: ReviewerResult gồm từng RuleCheck, observed/expected,
  source/confidence, trạng thái pass/fail/unknown, policy khoảng cách và scope.
- reviewer_validation.json: CLI lưu tóm tắt tại thư mục dự án.

ReviewIssue có rule_code, day_number/place_id/photo_id khi liên quan,
artifact_ids và target_agent. Sai schema cũng thành issue có agent chịu trách
nhiệm, không tạo PASS rỗng. Có major/blocker thì REVISE; chỉ có minor thì có thể
PASS. PASS mở task Layout Editor ready, REVISE chặn Layout và ghi feedback,
revision_target, dự án needs_attention. Reviewer task done nghĩa là đã kiểm tra,
không có nghĩa verdict PASS.

Không tự chạy lại agent bị yêu cầu sửa và chưa tạo vòng lặp revision. Không tăng
revision_count tự động. production_approved luôn false kể cả PASS: kết luận chỉ
đạt các luật dữ liệu đã định nghĩa. workflow.py fixture cũ giữ nguyên; có thể gọi
ReviewerAgent.run từ workflow thật sau VisualTeam.run khi visual_complete=true.

```powershell
python -m unittest discover -s tests -v
```

Các test PASS dùng dữ liệu tổng hợp để kiểm luật, không phải bằng chứng xác minh
ảnh/giá thật. Test REVISE gồm ngày đóng cửa, giờ nghỉ, thời điểm vượt một giây,
sai tiền tệ/limit, vượt một xu, thiếu/quá xa chặng, thiếu ảnh và metadata giả.

Đã xác nhận 18 test riêng của Reviewer đạt từ log chạy cuối. Lần kiểm tra toàn
bộ suite chưa có kết quả hoàn tất: thao tác lấy kết quả bị auto-review chặn do
tiến trình có telemetry tới endpoint Visual Studio với payload chưa xác định.
Không chạy lại hoặc đổi đường thực thi để bỏ qua chặn này. Báo cáo demo hiện
ghi REVISE với 20 vấn đề và giữ production_approved=false.
