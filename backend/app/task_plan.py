"""Fixed role dependencies from the TravelBook workflow, shared by PM and demo."""

DEPENDENCIES = {
    "pm": [], "researcher": ["pm"], "planner": ["researcher"], "budget": ["planner"],
    "content_editor": ["budget"], "image_curator": ["content_editor"],
    "illustrator": ["content_editor"], "map_agent": ["content_editor"],
    "reviewer": ["image_curator", "illustrator", "map_agent"],
    "layout_editor": ["reviewer"], "pm_finalize": ["layout_editor"],
}
TITLES = {
    "pm": "Chuẩn hóa yêu cầu và lập kế hoạch", "researcher": "Nghiên cứu địa điểm và thời tiết",
    "planner": "Lập lịch trình từng ngày", "budget": "Dự toán chi phí",
    "content_editor": "Biên tập nội dung cẩm nang", "image_curator": "Chọn ảnh thật và lưu giấy phép",
    "illustrator": "Tạo minh họa trang trí", "map_agent": "Tạo bản đồ và biểu đồ",
    "reviewer": "Kiểm duyệt sản phẩm", "layout_editor": "Dàn trang và xuất PDF",
    "pm_finalize": "PM xác nhận hoàn thành",
}


def build_tasks(project_id: str, *, descriptive_titles: bool = True):
    return [{"id": f"{project_id}:{node}", "title": TITLES[node] if descriptive_titles else node,
             "agent": "pm" if node == "pm_finalize" else node,
             "depends_on": [f"{project_id}:{previous}" for previous in previous_nodes],
             "order_index": index, "status": "todo"}
            for index, (node, previous_nodes) in enumerate(DEPENDENCIES.items())]
