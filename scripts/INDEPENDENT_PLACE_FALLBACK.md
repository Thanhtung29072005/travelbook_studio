# Researcher khi tất cả Overpass endpoint đều lỗi

Lỗi đã quan sát: Overpass trả HTTP 502 và lỗi kết nối/JSON ở các endpoint dự phòng. Thử lại cùng endpoint không xử lý được trường hợp cả chuỗi bị lỗi. Client dữ liệu trước đó dùng `trust_env=False`, trong khi môi trường kiểm tra có HTTP/HTTPS/ALL proxy.

Đã sửa:

- Giữ Overpass làm nguồn chính; nếu không có kết quả, thử Wikidata Query Service độc lập.
- Tìm entity có P625 trong bán kính cấu hình, thuộc nhóm điểm tham quan/bảo tàng/bãi biển/công viên/đền. SPARQL chỉ chứa số đã kiểm tra và QID cố định.
- Parse tên/tọa độ từ kết quả API, kiểm tra QID/loại/khoảng cách, loại trùng. Không dùng LLM tự tạo địa điểm, giá vé hay giờ mở cửa.
- Giữ source URL Wikidata, thời điểm lấy dữ liệu và confidence cho từng dữ kiện. Bảo toàn cảnh báo lỗi Overpass dù nguồn dự phòng thành công.
- Khi nguồn đang lỗi, không cho LLM lặp nguyên chuỗi tìm kiếm thêm một lần trong cùng lượt Researcher.
- Phân biệt timeout/kết nối theo loại lỗi với phản hồi không phải JSON, không ghi nguyên nội dung exception có thể chứa URL/token.
- Client dữ liệu có `DATA_TRUST_ENV=true` để dùng proxy hệ thống; có thể đặt false nếu muốn kết nối trực tiếp. Thêm dependency `httpx[socks]` để hỗ trợ ALL_PROXY dạng SOCKS, tránh lỗi thiếu socksio.

## Bằng chứng đã chạy

- Truy vấn chỉ đọc qua Wikidata: HTTP 200.
- Gọi **PublicAPIClient + WikidataPlaces thực tế** với tọa độ Nha Trang từ kết quả geocoder: **6 địa điểm hợp lệ** gồm Bãi Tre, Trần Phú Beach, Bảo tàng Alexandre Yersin, Nhà thờ Núi, Tháp Po Nagar, Chùa Long Sơn.
- Kết quả có source/confidence: `reference-review/wikidata-live-validation.json`.
- `test_independent_places.py`: 7 test đạt (chuyển nguồn, loại dữ liệu sai, không bịa khi tất cả nguồn lỗi, không lặp tìm kiếm, phân biệt lỗi mạng/JSON).
- `test_overpass_failover.py`: 5 test đạt.
- `test_researcher.py`: 13 test đạt.
- Kiểm tra cú pháp các file sửa đạt. Chưa xác nhận toàn bộ graph tới PDF trong lần kiểm tra này.

## Cập nhật và chạy lại

Trong đúng môi trường Python đang chạy backend:

```powershell
python -m pip install "httpx[socks]"
python -m backend.app.main
```

Dừng backend cũ trước khi khởi động lại, rồi bấm **Tiếp tục** trên dự án Nha Trang hiện có. Không xóa DB, cache hoặc đổi về dữ liệu mẫu. Nguồn dự phòng và tùy chọn proxy có giá trị mặc định nên `.env` cũ không bắt buộc sửa.

Kiểm tra riêng nguồn độc lập, không gọi model:

```powershell
python scripts/check_place_sources.py --destination "Nha Trang" --wikidata-only
```

Cấu hình tùy chọn:

```dotenv
WIKIDATA_QUERY_URL=https://query.wikidata.org/sparql
RESEARCH_WIKIDATA_FALLBACK_ENABLED=true
DATA_TRUST_ENV=true
```

Tài liệu API: https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual#SPARQL_endpoint và mục Geospatial search trên cùng trang. Nguồn dữ liệu vẫn có giới hạn coverage, rate limit và khả năng gián đoạn; không bảo đảm mọi điểm đến đều có đủ điểm cho mọi số ngày.
