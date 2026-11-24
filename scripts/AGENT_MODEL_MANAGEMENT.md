# Agent hỗ trợ và quản lý model

Tab **Agent** của mỗi dự án có thêm, sửa, bật/tắt và xóa agent. PM có thể sửa cấu hình model nhưng không thể tắt, xóa hoặc tạo bản PM thứ hai. API cũng áp dụng các ràng buộc này. Agent mới mặc định tắt; bật một hồ sơ sẽ tắt các hồ sơ khác cùng vai trò.

Hồ sơ gốc của các vai trò khác PM có thể tắt hoặc xóa. Xóa không làm mất artifacts/sự kiện đã tạo. Nếu không còn agent đang bật cho vai trò bắt buộc, workflow dừng tại vai trò đó với sự kiện `agent_unavailable`; không tự bỏ qua bước. Thêm và bật hồ sơ cùng vai trò để thay thế. Khi xóa hồ sơ tùy chỉnh đang bật, hồ sơ gốc được khôi phục nếu còn tồn tại.

Trang **Model** có đồng bộ 9Router, model chính, fallback văn bản theo thứ tự, model ảnh và fallback ảnh. Tab Agent chọn model riêng từ danh mục hoặc nhập ID thủ công. Illustrator dùng model ảnh và fallback của hồ sơ; Reviewer dùng trường ảnh/vision cho lần gọi vision. Các vai trò chạy bằng code có trường model bị khóa trên UI.

API:

- `GET/POST /api/projects/{id}/agents`: danh sách/thêm.
- `PATCH /api/projects/{id}/agents/{agent_id}`: sửa hoặc `{"enabled":false}` / `{"enabled":true}`.
- `DELETE /api/projects/{id}/agents/{agent_id}`: xóa, trừ PM.
- `GET /api/router/models`: đọc cấu hình/danh mục đã lưu, không gọi mạng.
- `POST /api/router/models/sync`: gọi `/models` của 9Router bằng key backend; HTTP lỗi/danh mục không hợp lệ giữ nguyên danh mục cũ.
- `PUT /api/router/models`: lưu `models` (phần tử đầu là chính), `image_model`, `image_fallback_models` vào SQLite; nạp lại khi mở backend.

Danh mục 9Router trả ID model; không có dữ liệu capability đáng tin cậy để tự phân loại model ảnh nên người dùng chọn model phù hợp. Đồng bộ không ghi đè lựa chọn đang chỉnh trên UI. Không lưu API key trong cấu hình model và không trả nội dung lỗi nhà cung cấp cho frontend.

Chạy ứng dụng trên máy của bạn:

```powershell
python -m backend.app.main
# terminal thứ hai
cd frontend
npm run dev
```

Test API mới:

```powershell
python -m unittest discover -s tests -p test_agent_model_management.py -v
```

Tình trạng kiểm chứng trong phiên này: frontend build hoàn tất; thao tác browser thêm agent và hiển thị model đã thành công trên API dùng thử. Thao tác bật tiếp theo không thực hiện được do Auto-review chặn preview khi phát hiện telemetry ngoài dự kiến. Không xác nhận các thao tác bật/tắt/xóa/sync/lưu model đã chạy end-to-end với backend thật. Bộ 6 test API đã viết nhưng chưa chạy vì hạn chế chạy backend/test trong phiên. Không khởi chạy lại server qua đường khác để vượt chặn.

`frontend/management-preview.mjs`, `management-test.html`, `src/ManagementPreview.tsx` là harness thử UI độc lập dùng dữ liệu mẫu trong bộ nhớ, không thuộc entrypoint sản phẩm. Request đã quan sát được ghi tại `reference-review/agent-model-ui-requests.json`. Supervisor lai trước đó giữ nguyên và không được bật thêm bởi task này.
