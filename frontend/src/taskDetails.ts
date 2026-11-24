import type { Task } from './types'

const TASK_DETAILS: Record<string, string[]> = {
  pm: ['Xác định điểm đến, ngày đi, số người và ngân sách.', 'Làm rõ sở thích và những yêu cầu còn thiếu.', 'Phân chia công việc và xác định thứ tự phụ thuộc giữa các agent.'],
  researcher: ['Tìm địa điểm, tọa độ, giờ mở cửa và thông tin giá vé.', 'Tra cứu thời tiết theo ngày khởi hành và phạm vi dự báo.', 'Lưu nguồn dữ liệu và ghi nhận những thông tin còn thiếu.'],
  planner: ['Sắp xếp các địa điểm thành lịch trình cho từng ngày.', 'Tính thời gian tham quan, di chuyển và các khoảng nghỉ.', 'Đối chiếu lịch trình với giờ mở cửa và độ tuổi người đi.'],
  budget: ['Tổng hợp chi phí ăn uống, lưu trú, di chuyển và tham quan.', 'Phân biệt giá có nguồn với các khoản ước tính.', 'Tính tổng dự toán và so sánh với ngân sách chuyến đi.'],
  content_editor: ['Viết phần giới thiệu chuyến đi và nội dung từng ngày.', 'Biên tập mẹo du lịch, gợi ý ăn uống và lưu ý văn hóa.', 'Đối chiếu nội dung với lịch trình và dữ liệu đã thu thập.'],
  image_curator: ['Tìm và chọn ảnh chụp phù hợp với từng địa điểm.', 'Kiểm tra thông tin tác giả, nguồn ảnh và giấy phép.', 'Tải ảnh để đưa vào cẩm nang và lưu thông tin ghi công.'],
  illustrator: ['Xây dựng mô tả hình ảnh theo phong cách và bảng màu chung.', 'Tạo ảnh bìa, minh họa từng ngày và các chi tiết trang trí.', 'Lưu bộ ảnh minh họa và thông tin tạo ảnh.'],
  map_agent: ['Vẽ sơ đồ các điểm tham quan theo từng ngày.', 'Tạo biểu đồ ngân sách và thời tiết từ dữ liệu hiện có.', 'Dựng biểu đồ thời gian cho toàn bộ lịch trình.'],
  reviewer: ['Đối chiếu lịch trình, chi phí và mức độ đầy đủ của dữ liệu.', 'Kiểm tra ảnh, phong cách minh họa và độ phù hợp với người đi.', 'Gửi yêu cầu chỉnh sửa hoặc chuyển sang bước dàn trang.'],
  layout_editor: ['Kết hợp nội dung, ảnh minh họa, bản đồ và biểu đồ.', 'Dàn mục lục, bảng lịch trình, ngân sách và số trang.', 'Xuất PDF và kiểm tra bố cục văn bản, hình ảnh.'],
  pm_finalize: ['Kiểm tra file PDF đã được tạo.', 'Lưu sản phẩm cuối để người dùng tải xuống.', 'Cập nhật trạng thái hoàn tất của dự án.'],
}

export function taskDetails(task: Task): string[] {
  const step = task.step_key || task.id.split(':').at(-1) || task.agent
  return TASK_DETAILS[step] || TASK_DETAILS[task.agent] || []
}

export function childTasks(task: Task, tasks: Task[]): Task[] {
  return tasks.filter(child => child.parent_id === task.id).sort((a, b) => a.order_index - b.order_index)
}
