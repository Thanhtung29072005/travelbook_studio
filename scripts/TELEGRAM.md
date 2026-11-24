# Nhận thông báo agent và PDF qua Telegram

TravelBook gửi tin khi từng agent hoàn thành và gửi file PDF sau khi dự án hoàn tất.
Các agent chạy song song báo theo thứ tự hoàn thành thực tế. Reviewer báo rõ khi còn cần sửa; hoàn thành một lượt agent không có nghĩa dự án đã hoàn tất.
Vòng sửa có nhãn riêng. PDF chỉ gửi sau bước PM xác nhận cuối, không gửi PDF trung gian lúc chờ duyệt.

## 1. Tạo bot

1. Trong Telegram, mở bot chính thức **@BotFather**, chọn `/newbot`.
2. Đặt tên và username cho bot, lưu token BotFather cấp vào `.env` ở thư mục gốc TravelBook:

```dotenv
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Điền token sau `TELEGRAM_BOT_TOKEN=`. Không đưa token vào GitHub, `.env.example`, ảnh chụp màn hình hay tin nhắn công khai.

## 2. Chọn nơi nhận

- Nhận riêng: mở cuộc trò chuyện với bot vừa tạo, bấm Start hoặc gửi `/start`.
- Nhận trong nhóm: thêm bot vào nhóm, cho phép bot gửi tin nhắn/tài liệu, rồi gửi `/start@username_bot` trong nhóm.

Tại thư mục gốc TravelBook, chạy:

```powershell
.\.venv\Scripts\python.exe scripts/check_telegram.py --list-chats
```

Chọn `chat_id` đúng cuộc trò chuyện và điền vào `.env`. Giữ dấu âm nếu ID nhóm có dấu âm.
Lệnh đọc các cập nhật gần đây của bot và có thể liệt kê nhiều chat: đối chiếu `name` và `type` trước khi chọn.
Nếu không thấy, gửi lại `/start` rồi chạy lại. Một chương trình khác đang đọc update có thể đã lấy mất cập nhật; bot có webhook cũng không dùng được getUpdates. Không xóa webhook của bot đang dùng cho dịch vụ khác; nên tạo bot riêng cho TravelBook.

## 3. Thử kết nối rồi bật thông báo

```powershell
.\.venv\Scripts\python.exe scripts/check_telegram.py --test
```

Khi nhận được tin thử ở đúng cuộc trò chuyện, bật:

```dotenv
TELEGRAM_ENABLED=true
```

Khởi động lại backend:

```powershell
.\.venv\Scripts\python.exe -m backend.app.main
```

Tạo hoặc tiếp tục dự án. Tin nhắn có tên dự án, mã dự án, agent và số vòng sửa nếu có.
Nếu dùng topic trong nhóm Telegram, thêm `TELEGRAM_MESSAGE_THREAD_ID=` với ID topic thích hợp. Công cụ liệt kê cũng hiển thị `topic_id` nếu bản cập nhật có trường đó.

## Cách gửi và giới hạn

- Backend chủ động gọi Telegram qua HTTPS, không nhận lệnh từ bot; không cần webhook, mở cổng Internet hoặc Radmin cho riêng Telegram.
- Máy chạy TravelBook phải có Internet và backend đang mở. Kết nối tới 9Router vẫn cấu hình riêng như trước.
- Gửi chạy bằng hàng đợi nền trong SQLite. Lỗi Telegram không làm agent/dự án thất bại. Tự thử tối đa ba lần, lưu trạng thái khi backend khởi động lại.
- Không gửi lại những mục đã được Telegram xác nhận thành công khi tiếp tục dự án. Nếu mất mạng đúng sau khi Telegram đã nhận nhưng trước khi trả lời, lần thử lại vẫn có thể tạo bản trùng; Bot API không bảo đảm gửi đúng một lần trong tình huống đó.
- Các mục đang chờ được gửi lại khi backend mở, không phát lại toàn bộ lịch sử agent trước khi bật tính năng.
- Xem sự kiện `telegram_sent`/`telegram_failed` trong phần Giao tiếp. Sau ba lần lỗi, mục đó ngừng tự thử; kiểm tra key, quyền bot và nơi nhận.
- File PDF dùng `sendDocument`; PDF vượt 50 MB chưa được gửi qua API cloud này. File vẫn có trong Sản phẩm.
- Tất cả dự án trên một backend gửi tới `TELEGRAM_CHAT_ID` đã cấu hình. Nếu mỗi thành viên chạy backend riêng, mỗi người có thể chọn bot/chat riêng; dùng chung nhóm thì phân biệt bằng tên và mã dự án.
- Lỗi 401/403: kiểm tra token, người dùng đã Start bot, bot chưa bị chặn và có quyền gửi trong nhóm. Lỗi 429: giới hạn Telegram; hàng đợi chờ rồi thử lại.
- Token được đọc từ `.env`, không lưu vào hàng đợi và không ghi URL chứa token vào sự kiện lỗi.

Tắt thông báo bằng `TELEGRAM_ENABLED=false`, sau đó khởi động lại backend.
