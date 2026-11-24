"""Generate and validate offline fixtures, not verified travel recommendations.

Run from project root with Python + Pydantic 2:
    python scripts/generate_danang_sample.py
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import TypeAdapter  # noqa: E402

from backend.app.schemas import Budget, DayPlan, Fact, Photo, Place  # noqa: E402

OUTPUT = ROOT / "backend/data/artifacts/danang_3days"
DEMO = "fixture:danang_3days/v1; dữ liệu demo tự soạn, chưa xác minh thực tế"


def fact(value, label, confidence=0.3):
    return {"value": value, "source": f"{DEMO}; {label}", "confidence": confidence}


def build_samples():
    places = []
    # Coordinates are approximate fixtures, not an OSM/route API response.
    rows = [
        ("my_khe", "Bãi biển Mỹ Khê", 16.0544, 108.2470, 90),
        ("cham_museum", "Bảo tàng Điêu khắc Chăm Đà Nẵng", 16.0609, 108.2233, 90),
        ("dragon_bridge", "Cầu Rồng", 16.0611, 108.2275, 45),
        ("linh_ung", "Chùa Linh Ứng, bán đảo Sơn Trà", 16.1003, 108.2778, 90),
        ("son_tra_stop", "Điểm ngắm cảnh giả lập trên tuyến Sơn Trà", 16.1080, 108.2780, 60),
        ("han_market", "Chợ Hàn", 16.0675, 108.2240, 90),
        ("marble_mountains", "Danh thắng Ngũ Hành Sơn", 16.0035, 108.2642, 120),
        ("non_nuoc", "Bãi biển Non Nước", 15.9980, 108.2760, 60),
    ]
    for place_id, name, lat, lon, duration in rows:
        places.append({
            "id": place_id,
            "name": fact(name, "tên địa điểm minh họa"),
            "latitude": fact(lat, "tọa độ xấp xỉ, không dùng để dẫn đường"),
            "longitude": fact(lon, "tọa độ xấp xỉ, không dùng để dẫn đường"),
            "description": fact(
                "Địa điểm trong kịch bản demo; cần Researcher xác minh trước khi xuất cẩm nang thật.",
                "ghi chú dữ liệu mẫu", 1.0,
            ),
            "opening_hours": None,
            "ticket_price": None,
            "currency": None,
            "visit_minutes": fact(duration, "thời lượng dự kiến trong kịch bản", 0.5),
            "age_notes": [],
        })

    def stop(place_id, arrival, departure, travel):
        return {
            "place_id": place_id,
            "arrival": fact(arrival, "giờ đến đề xuất; giờ địa phương Asia/Ho_Chi_Minh", 0.6),
            "departure": fact(departure, "giờ rời đi đề xuất; giờ địa phương Asia/Ho_Chi_Minh", 0.6),
            "travel_minutes": fact(travel, "thời gian di chuyển giả lập, chưa gọi routing API"),
            "distance_km": None,
            "notes": [fact("Chưa xác minh giờ mở cửa; điều chỉnh sau khi có dữ liệu thực tế.",
                           "giới hạn fixture", 1.0)],
        }

    itinerary = [
        {
            "day_number": 1, "date": None,
            "title": fact("Ngày 1 — Biển và trung tâm thành phố", "tiêu đề biên tập", 0.8),
            "stops": [stop("my_khe", "07:00", "08:30", 0),
                      stop("cham_museum", "09:15", "10:45", 30),
                      stop("dragon_bridge", "16:00", "16:45", 15)],
            "weather": [],
            "tips": [fact("Khoảng trống buổi trưa dành cho ăn uống và nghỉ ngơi.",
                          "quy ước lịch trình demo", 1.0)],
        },
        {
            "day_number": 2, "date": None,
            "title": fact("Ngày 2 — Sơn Trà và chợ Hàn", "tiêu đề biên tập", 0.8),
            "stops": [stop("linh_ung", "08:00", "09:30", 0),
                      stop("son_tra_stop", "10:00", "11:00", 20),
                      stop("han_market", "15:00", "16:30", 45)],
            "weather": [],
            "tips": [fact("Điểm ngắm cảnh là điểm giả lập; thay bằng địa điểm đã xác minh trước khi sử dụng.",
                          "giới hạn fixture", 1.0)],
        },
        {
            "day_number": 3, "date": None,
            "title": fact("Ngày 3 — Ngũ Hành Sơn và Non Nước", "tiêu đề biên tập", 0.8),
            "stops": [stop("marble_mountains", "08:00", "10:00", 0),
                      stop("non_nuoc", "10:30", "11:30", 20)],
            "weather": [],
            "tips": [fact("Buổi chiều để trống cho việc trả phòng và rời Đà Nẵng.",
                          "quy ước lịch trình demo", 1.0)],
        },
    ]
    cost_rows = [
        ("hotel", "accommodation", "Một người lớn, một phòng × 2 đêm × 600.000 VND", "1200000", None),
        ("food1", "food", "Ăn uống ngày 1 cho một người lớn", "300000", 1),
        ("food2", "food", "Ăn uống ngày 2 cho một người lớn", "300000", 2),
        ("food3", "food", "Ăn uống ngày 3 cho một người lớn", "300000", 3),
        ("transport1", "transport", "Tổng di chuyển nội thành ngày 1", "200000", 1),
        ("transport2", "transport", "Tổng di chuyển ngày 2", "400000", 2),
        ("transport3", "transport", "Tổng di chuyển ngày 3", "300000", 3),
        ("tickets1", "tickets", "Khoản dự trù vé ngày 1, không phải báo giá điểm tham quan", "100000", 1),
        ("tickets3", "tickets", "Khoản dự trù vé ngày 3, không phải báo giá điểm tham quan", "100000", 3),
        ("reserve", "contingency", "Quỹ dự phòng cho toàn chuyến", "500000", None),
    ]
    budget = {
        "currency": fact("VND", "đơn vị tiền quy ước trong demo", 1.0),
        "limit": fact("5000000", "ngân sách giả định cho một người lớn", 1.0),
        "items": [
            {"id": row_id, "category": category,
             "description": fact(description, "phạm vi khoản dự trù", 1.0),
             "amount": fact(amount, "ước tính demo, không có báo giá nhà cung cấp"),
             "price_kind": "estimated", "day_number": day}
            for row_id, category, description, amount, day in cost_rows
        ],
    }
    content = [
        fact("Cẩm nang mẫu Đà Nẵng 3 ngày dành cho một người lớn, ngân sách 5.000.000 VND.",
             "brief giả định của fixture", 1.0),
        fact("Ngày 1: mở đầu với Mỹ Khê, tiếp tục tới Bảo tàng Điêu khắc Chăm và kết thúc tại Cầu Rồng.",
             "biên tập từ itinerary.json ngày 1", 0.8),
        fact("Ngày 2: lịch demo kết hợp chùa Linh Ứng, một điểm ngắm cảnh giả lập và chợ Hàn.",
             "biên tập từ itinerary.json ngày 2", 0.8),
        fact("Ngày 3: dành buổi sáng cho Ngũ Hành Sơn và Non Nước; buổi chiều chủ động sắp xếp rời thành phố.",
             "biên tập từ itinerary.json ngày 3", 0.8),
        fact("Có thể dành các bữa ăn để thử mì Quảng, bánh tráng cuốn thịt heo hoặc hải sản theo sở thích.",
             "gợi ý biên tập demo, không phải kết quả tìm nhà hàng", 0.3),
        fact("Ngân sách chỉ bao gồm khoản tại điểm đến; chưa tính vé máy bay hoặc tàu xe đến và rời Đà Nẵng, mua sắm cá nhân.",
             "phạm vi dự toán demo", 1.0),
        fact("Chưa chọn ngày đi nên không có dự báo thời tiết. Giờ mở cửa và giá vé đều chưa xác minh.",
             "trạng thái dữ liệu fixture", 1.0),
        fact("Mọi ảnh trong photos.json là placeholder, chưa có tác giả hoặc giấy phép được xác minh; không tải hoặc đưa vào PDF thật.",
             "trạng thái nguồn ảnh fixture", 1.0),
    ]
    photos = [
        {
            "id": f"photo_{place_id}", "place_id": place_id,
            "url": fact(f"https://example.invalid/danang/{place_id}.jpg", "placeholder URL, không có ảnh thật", 0),
            "author": fact("PLACEHOLDER — chưa xác minh tác giả", "không có dữ liệu tác giả", 0),
            "license": fact("UNVERIFIED — chưa có quyền sử dụng ảnh", "không có dữ liệu giấy phép", 0),
            "source_url": fact(f"https://example.invalid/danang/{place_id}", "placeholder trang nguồn", 0),
            "caption": fact(f"Ảnh minh họa cần tìm cho {name}", "nhãn yêu cầu ảnh demo", 1.0),
            "license_url": None, "local_path": None,
        }
        for place_id, name, *_ in rows
    ]
    return {
        "research.json": (places, TypeAdapter(list[Place])),
        "itinerary.json": (itinerary, TypeAdapter(list[DayPlan])),
        "budget.json": (budget, TypeAdapter(Budget)),
        "content.json": (content, TypeAdapter(list[Fact[str]])),
        "photos.json": (photos, TypeAdapter(list[Photo])),
    }


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    validated = {}
    for filename, (payload, adapter) in build_samples().items():
        model = adapter.validate_python(payload)
        # Budget total/within_budget are read-only computed fields: omit them
        # from the input fixture so Budget.model_validate_json can load it again.
        exclude = {"total", "within_budget"} if filename == "budget.json" else None
        data = adapter.dump_python(model, mode="json", exclude=exclude)
        path = OUTPUT / filename
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        validated[filename] = adapter.validate_json(path.read_text(encoding="utf-8"))
        print(f"VALID {filename}")

    places = validated["research.json"]
    days = validated["itinerary.json"]
    photos = validated["photos.json"]
    budget = validated["budget.json"]
    place_ids = {place.id for place in places}
    if len(place_ids) != len(places):
        raise ValueError("duplicate place IDs")
    if [day.day_number for day in days] != [1, 2, 3]:
        raise ValueError("expected exactly three consecutive days")
    for day in days:
        for stop in day.stops:
            if stop.place_id not in place_ids:
                raise ValueError(f"unknown place: {stop.place_id}")
    if {photo.place_id for photo in photos} != place_ids:
        raise ValueError("each place needs one placeholder photo")
    for item in budget.items:
        if item.day_number is not None and item.day_number not in {1, 2, 3}:
            raise ValueError("budget references an unknown day")
    if budget.total.value != Decimal("3700000") or not budget.within_budget.value:
        raise ValueError("unexpected sample budget")
    print(f"VALID cross-file references; total={budget.total.value} VND; limit={budget.limit.value} VND")


if __name__ == "__main__":
    main()
