# Image Curator và nguồn ảnh

```powershell
python scripts/check_image_curator.py --mock
python scripts/check_image_curator.py --timeout 10
python scripts/check_image_curator.py --research path/to/research.json --trip-request path/to/trip.json
```

Đầu vào research.json là list[Place]. Nếu có TripRequest, tên điểm đến được
thêm vào truy vấn; không tìm ảnh chung của thành phố để thay cho địa điểm bị thiếu.
CLI khởi tạo các task trước đó dưới dạng demo, không thực thi các agent trước.

## Kết quả

Mỗi lần chạy có thư mục riêng dưới backend/data/artifacts/<project_id>/image_curator/<run_id>:

- photos.json: list[Photo], tối đa một ảnh cho mỗi Place, có author, license,
  source_url, license_url, provider, caption và selection_note. Các giá trị nghiệp
  vụ dùng Fact với source/confidence; local_path trống vì đây là bước chọn URL.
- attribution.json: danh sách AttributionRecord, gồm dòng ghi công, liên kết
  trang ảnh, giấy phép, hồ sơ tác giả (nếu có), yêu cầu ghi công riêng và hạn chế.
- photo_report.json: ảnh đã chọn, địa điểm còn thiếu và cảnh báo từng nguồn.
- attribution.txt và photos_validation.json: CLI xuất để đọc/kiểm tra nhanh.

CLI trả 0 khi mọi địa điểm có một ảnh, 2 khi còn thiếu. Khi thiếu, task bị blocked,
dự án needs_attention, không tự tạo Photo placeholder hay gán license cho nền màu.
Ảnh được chọn theo metadata/tên, chưa xác nhận đúng địa điểm bằng thị giác;
production_approved luôn false. Ảnh có license metadata không đồng nghĩa các
quyền về người mẫu, nhãn hiệu hay vật thể trong ảnh đã được kiểm chứng.

## Nguồn và lựa chọn

Thứ tự mặc định: Wikimedia -> Unsplash -> Pexels. Thử nguồn kế tiếp khi API lỗi,
thiếu khóa hoặc không có ứng viên phù hợp. Mặc định lấy tối đa 6 kết quả mỗi
nguồn/địa điểm, cần ít nhất 800x450 px và khớp ít nhất 75% token tên địa điểm.
Chuẩn hóa dấu tiếng Việt, chọn mức khớp cao nhất rồi ưu tiên độ phân giải; không
tái dùng cùng provider ID cho nhiều địa điểm trong một lần curate.

Wikimedia không cần API key. Imageinfo trả Artist, Credit, Attribution,
LicenseShortName, LicenseUrl, Restrictions và UsageTerms; không dùng người upload
thay cho tác giả. HTML được chuyển thành văn bản, giữ liên kết tác giả khi có.
Chỉ chấp nhận tập giấy phép rõ: CC BY/CC BY-SA 1.0, 2.0, 2.5, 3.0, 4.0;
CC0 hoặc Public Domain Mark với URL tương ứng. Tên/URL mâu thuẫn, giấy phép
khác hoặc thiếu tác giả đều bị bỏ qua, kể cả ảnh có thể hợp lệ ngoài tập hỗ trợ.
Giữ ghi công tùy chỉnh và yêu cầu ShareAlike; bước sử dụng/chỉnh sửa sau đó cần
ghi nhận thay đổi, không chỉ xuất tên giấy phép.

Unsplash cần UNSPLASH_ACCESS_KEY; Pexels cần PEXELS_API_KEY trong .env. Khóa chỉ
được gửi qua header, không đưa vào URL hay source. Cache SQLite phân tách theo
hash header. Các URL ảnh, trang nguồn, tác giả và endpoint tracking được kiểm tra
HTTPS/host trước khi sử dụng, không gửi khóa đến URL tracking ngoài Unsplash.

Unsplash giữ nguyên photo.urls.regular để hotlink, thêm UTM vào trang ảnh/hồ sơ
tác giả và gọi links.download_location khi chọn ảnh (không lấy từ cache).
Tracking lỗi thì bỏ ảnh và chuyển ứng viên/nguồn. Không tải lại ảnh Unsplash
để nhúng local vào PDF: renderer hiện chặn nhánh local đối với provider này.
Muốn hiển thị ảnh đó cần giao diện hỗ trợ hotlink hoặc thỏa thuận phù hợp.

Pexels lấy photographer, photographer_url, url, src.original và áp dụng Pexels
License theo chính sách nguồn. Giữ ghi công Pexels và tác giả theo hướng dẫn API.
Không đổi các giấy phép nguồn thành “CC0” hoặc “miễn phí không điều kiện”.

## Tích hợp

PhotoService.select(place, destination, used=...) trả PhotoSearchResult.
ImageCuratorAgent.curate(places, destination) trả CurationResult. run(State)
yêu cầu task Content Editor phụ thuộc đã done, lưu artifact/sự kiện SQLite,
cập nhật State.photos/photo_warnings. Reviewer chỉ ready khi cả Image Curator,
Illustrator và Map Agent đã done. Module không gọi LLM và không tải bytes ảnh.
workflow.py vẫn là đồ thị fixture; việc nối toàn bộ agent thật là bước riêng.

LayoutService giữ các trường ghi công/URL giấy phép/yêu cầu riêng trong trang
nguồn ảnh. Việc chọn ảnh từ xa chưa làm ảnh tự xuất hiện trong PDF; ảnh Wikimedia
hoặc Pexels cần bước tải/chuẩn bị ảnh trước khi gán local_path.

## Kiểm chứng

```powershell
python -m unittest discover -s tests -v
```

Demo --mock tạo 8/8 hồ sơ từ HTTP giả, với tác giả/credit ghi rõ MOCK: đây không
phải bằng chứng có 8 ảnh thật hoặc giấy phép thực tế. Lần gọi API thật trong môi
trường kiểm tra không lấy được ảnh; Wikimedia báo ConnectError, Unsplash có khóa
nhưng chưa truy cập được kết quả hợp lệ, Pexels chưa có khóa. Báo cáo thật giữ
đủ danh sách thiếu, không đánh dấu hoàn thành giả.

Tài liệu chính thức đã đối chiếu khi triển khai:

- https://www.mediawiki.org/wiki/API:Imageinfo
- https://www.mediawiki.org/wiki/Extension:CommonsMetadata
- https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia
- https://unsplash.com/documentation
- https://help.unsplash.com/en/articles/2511258-guideline-triggering-a-download
- https://www.pexels.com/api/documentation/
- https://www.pexels.com/license/
