# Supervisor lai

`backend/app/agents/supervisor.py` kết hợp code kiểm soát thứ tự với LLM chọn agent. Bật trong `.env`, rồi khởi động lại backend:

```dotenv
SUPERVISOR_ENABLED=true
SUPERVISOR_FALLBACK_ENABLED=true
```

Dùng API key và chuỗi model `NINE_ROUTER_MODELS` hiện có. Cấu hình mặc định trong code giữ `SUPERVISOR_ENABLED=false` để tương thích đồ thị cũ; `.env.example` đã bật tính năng cho cài đặt mới.

Code tạo frontier từ các cạnh hợp lệ của graph. Supervisor lọc frontier bằng dependency chuẩn, dependency task trong DB, trạng thái task và hồ sơ agent đang bật. Dependency chuẩn không thể bị nới lỏng chỉ bằng cách sửa metadata task. Không có ứng viên thì graph dừng với `supervisor_blocked`, không hỏi LLM để tự bỏ qua bước.

Mỗi node thực thi kiểm tra lại dependency, kể cả khi đi vào bằng resume hoặc nhánh sửa bắt buộc. Ví dụ yêu cầu chạy Reviewer trước khi đủ ba visual sẽ thất bại trước khi node bắt đầu, thay vì bỏ qua các agent còn thiếu.

LLM nhận danh sách hợp lệ và trả `agents` cùng `reason` có source/confidence. Schema Pydantic được tạo cho từng lượt với enum chỉ chứa các ứng viên; cấm danh sách trống, tên lạ và chọn trùng. Sau parse, code kiểm tra eligibility lần nữa trước khi dùng `Send`.

Thứ tự được giữ: PM, Researcher, Planner, Budget, Content Editor, nhóm visual, Reviewer, Layout Editor, PM finalize. LLM có thể chọn cả ba visual để chạy song song hoặc chọn từng phần. Những visual chưa chọn được giữ trong checkpoint; Reviewer chỉ chạy khi Image Curator, Illustrator và Map Agent đều hoàn tất. Trong các đoạn chỉ có một agent hợp lệ, LLM không có quyền chọn agent ở bước sau.

Điểm duyệt lịch trình/ảnh/PDF vẫn gọi `interrupt`. Chưa duyệt thì không gọi supervisor để chạy bước tiếp theo. Sửa thủ công và sửa theo Reviewer là các nhánh bắt buộc do code định tuyến; sau khi sửa graph quay lại quy trình kiểm duyệt và lập lịch. Giới hạn sửa tự động `MAX_REVISIONS` giữ nguyên.

9Router xử lý model chính → model dự phòng, schema và cache như các agent khác. Nếu toàn bộ model thất bại hoặc output không hợp lệ, code có thể chọn chính tập ứng viên hợp lệ và ghi `mode=code_fallback`; đặt `SUPERVISOR_FALLBACK_ENABLED=false` để dừng khi LLM lỗi. Không có chế độ nào cho phép chọn agent ngoài tập hợp lệ hoặc kết thúc graph sớm.

## Chạy kiểm tra

```powershell
python scripts/check_supervisor.py
python scripts/check_supervisor.py --serial
python -m unittest discover -s tests -p test_supervisor.py -v
```

Hai demo mặc định dùng HTTP mock qua client 9Router thật, structured output và StateGraph thật; không tốn API. Demo ghi `supervisor_validation.json`, kiểm tra dependency tại mọi sự kiện bắt đầu, mỗi node hoàn thành một lần và tạo PDF.

Để chỉ supervisor gọi model thật:

```powershell
python scripts/check_supervisor.py --live
```

Các worker của graph hiện vẫn đọc JSON mẫu. `--live` chỉ chuyển quyết định điều phối sang 9Router, không biến worker thành agent dữ liệu thật. Nếu model lỗi, kiểm tra `supervisor_decision.mode` để phân biệt LLM và code fallback. Phiên này xác minh bằng mock/ASGI và graph integration, không kiểm thử nhà cung cấp thật hoặc thao tác trình duyệt.

## Xem quyết định

API chi tiết dự án chứa `state.supervisor_trace`; tab sự kiện có `supervisor_decision`, `supervisor_blocked`, `supervisor_failed`, `model_call` và `model_fallback`. Mỗi quyết định ghi eligible_agents, selected_agents, reason, model, cache_hit hoặc error_type. Không ghi API key hay nội dung phản hồi lỗi của nhà cung cấp.
