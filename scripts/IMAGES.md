# Style lock và xử lý ảnh

`style_lock.py` đọc `IMAGE_STYLE_LOCK` trong Settings; để trống sẽ dùng phong cách mặc định.
`get_style_lock()` trả một StyleLock bất biến; `signature` là SHA-256 của style và quy tắc chung.
`build_prompt()` chèn cùng style và yêu cầu không vẽ chữ, bản đồ hay biểu đồ dữ liệu.
Style lock là ràng buộc prompt, không bảo đảm model tạo ảnh giống phong cách tuyệt đối.

```python
from backend.app.services.style_lock import get_style_lock
from backend.app.services.images import resize_image, crop_image, placeholder_image, prepare_image

lock = get_style_lock()
cover_prompt = lock.apply("Decorative cover for a three-day Da Nang travel guide")
day_prompt = lock.apply("Decorative opening illustration for day one")

resized = resize_image("photo.jpg", (640, 360))
cropped = crop_image("photo.jpg", (300, 300), centering=(0.5, 0.5))
placeholder = placeholder_image((800, 450), background="#F3EBDD")
result = prepare_image(None, (640, 360))
assert result.is_placeholder
assert result.image.size == (640, 360)
result.image.save("fallback.png")
```

| Hàm | Hành vi |
| --- | --- |
| resize_image | Giữ toàn bộ ảnh và tỷ lệ; thêm viền nền để đạt đúng kích thước |
| crop_image | Cắt và resize để lấp đầy khung, giữ tỷ lệ; hỗ trợ box và centering |
| placeholder_image | Tạo ảnh nền màu đặc đúng kích thước, không có chữ |
| prepare_image | Chọn contain/cover/stretch; trả ImageResult và cờ ảnh dự phòng |

Mọi kích thước là `(width, height)` tính bằng pixel, phải là số nguyên dương.
Các hàm nhận Pillow Image, đường dẫn local hoặc bytes ảnh đã mã hóa.
Đầu ra là đối tượng Pillow RGB mới; không sửa ảnh gốc. EXIF orientation được xử lý trước.
Vùng trong suốt được ghép lên màu nền đã chọn để dùng cho PDF/JPEG.
box được tính trên ảnh sau khi xoay EXIF và phải nằm trọn trong ảnh.

prepare_image trả nền dự phòng khi thiếu/hỏng ảnh; fallback=False sẽ phát sinh lỗi nguồn.
Kích thước, màu nền hoặc mode không hợp lệ luôn phát sinh lỗi, không bị che bằng ảnh dự phòng.
Source/output giới hạn 40 triệu pixel. Hàm không tải URL hoặc gọi model tạo ảnh.
Ảnh dự phòng cần được agent ghi nhận là placeholder, không phải ảnh thật có nguồn/giấy phép.

```powershell
python scripts/check_images.py
python -m unittest discover -s tests -p test_images.py -v
```

Demo tạo ảnh kiểm thử bằng các vùng màu, lưu tại backend/data/artifacts/image_smoke,
mở lại từng PNG để kiểm tra kích thước và ghi validation.json.
