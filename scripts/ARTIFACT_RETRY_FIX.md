# Sửa lỗi UNIQUE constraint failed: artifacts.id

Nguyên nhân đã đối chiếu code/DB: `Database.add_artifact` luôn INSERT, trong khi workflow có ID ổn định theo dự án/vòng chạy/đường dẫn. Researcher retry ghi lại báo cáo lỗi với cùng ID. Ngoài ra các agent Map/Illustrator/Layout có thể đăng ký artifact trước khi wrapper graph đăng ký lại cùng object.

Sửa:

- Ghi artifact trong transaction BEGIN IMMEDIATE. Cùng ID và cùng metadata được coi là đăng ký lặp hợp lệ; giữ created_at ban đầu.
- Cùng ID nhưng khác dự án, đường dẫn, agent, source/confidence hoặc metadata khác vẫn bị chặn bằng lỗi rõ ràng. Không dùng INSERT OR REPLACE để ghi đè bất kỳ bản ghi nào.
- Factory artifact của workflow dùng lại created_at nếu ID đã có, tránh xung đột reducer chỉ do timestamp khi retry.
- Báo cáo lỗi Researcher mỗi lần ghi vào `diagnostics/<uuid>/research_diagnostics.json`, giữ lịch sử chẩn đoán trước đó.

Đã chạy riêng test DB với SQLite tạm, không server/LLM/provider:

```powershell
python -m unittest discover -s tests -p test_db.py -v
```

Kết quả: **13 tests, OK**, bao gồm đăng ký lặp, 8 lượt đăng ký cùng artifact qua 4 thread, và chặn ghi đè khác dự án/file/provenance. Log tại `reference-review/artifact-idempotency-tests.log`. Kiểm tra cú pháp các file sửa cũng đạt. Không xác nhận kết quả chạy lại toàn workflow/provider trong phiên này.

Khởi động lại backend, rồi bấm **Tiếp tục** tại dự án đang lỗi. Không xóa DB hay artifacts. Nếu nguồn dữ liệu vẫn lỗi, hệ thống sẽ giữ thông báo nguồn dữ liệu cùng báo cáo riêng, thay vì lỗi trùng ID che mất nguyên nhân.
