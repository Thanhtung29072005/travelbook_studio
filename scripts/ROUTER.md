# 9Router: structured output và cache

Từ thư mục gốc, cài các dependency đã khai báo trong pyproject.toml:

```powershell
python -m pip install -e .
```

Cấu hình `.env` với `NINE_ROUTER_BASE_URL`, `NINE_ROUTER_API_KEY` và
`NINE_ROUTER_MODELS=model-chinh,model-du-phong-1,model-du-phong-2`.
Dùng ID model thật từ catalog 9Router của bạn. `.env.example` có các tùy chọn cache.
File `.env` hiện có không bị thay đổi.

```python
from backend.app.config import get_settings
from backend.app.schemas import Fact
from backend.app.services.router import NineRouterClient

async def example():
    async with NineRouterClient(get_settings()) as router:
        args = dict(
            system="Return a fact with value, source and confidence.",
            prompt="Return value TravelBook Studio, source user:demo, confidence 1.",
            schema=Fact[str],
            temperature=0,
        )
        first = await router.chat_structured(**args)
        second = await router.chat_structured(**args)
        print(first.cache_hit, second.cache_hit)  # False, True on a fresh cache
        print(second.data.value)
```

Kiểm tra thật (gọi API đã cấu hình và có thể tính phí):

```powershell
python scripts/check_router_cache.py
```

Demo offline (HTTP giả lập, chạy pipeline LangChain và SQLite thật):

```powershell
python scripts/check_router_cache.py --mock
python -m unittest discover -s tests -v
```

Kết quả demo mong đợi: `Call 1: cache_hit=False`, `Call 2: cache_hit=True`,
`HTTP requests to simulated 9Router: 1`.

## Chính sách

- `chat_structured` trả `StructuredResult` gồm model Pydantic đã xác thực,
  model thực tế, cache_hit, cache_key và usage. `chat_json` nhận cùng tham số
  và trả `(dict, metadata)` để các agent sử dụng.
- Pipeline dùng `ChatPromptTemplate`, `RunnableLambda`, `PydanticOutputParser`
  của LangChain. HTTP dùng httpx, tương thích endpoint `/chat/completions` của 9Router.
- Mặc định gửi `response_format: json_object`. Chọn `json_schema` nếu provider
  hỗ trợ JSON Schema, hoặc `prompt` nếu provider không hỗ trợ response_format.
  Cả ba chế độ đều xác thực đầu ra bằng Pydantic tại backend.
- Model chính luôn đứng đầu `NINE_ROUTER_MODELS`; tiếp đến các fallback.
  Override `models=[...]` thay thế toàn bộ chuỗi cho một lần gọi.
- HTTP 402/404/429 chuyển ngay model tiếp theo. Lỗi mạng, timeout, 5xx hoặc JSON/schema
  sai được thử lại theo `LLM_MAX_ATTEMPTS_PER_MODEL`, sau đó chuyển model.
  HTTP 401/403 và các lỗi request không thể thử lại dừng ngay.
- Chỉ output hợp lệ được cache. Cache lỗi JSON, hết TTL hoặc không còn hợp schema
  sẽ bị xóa và gọi lại. Lỗi request không được cache.
- SHA-256 gồm prompt, system prompt, schema, model chain, temperature, chế độ JSON,
  endpoint và dấu vân tay tài khoản; không lưu prompt/API key thô trong SQLite.
  Cache có chứa nội dung đầu ra của model.
- Cache nằm tại `backend/data/llm_cache.sqlite3`, TTL mặc định 86400 giây.
  Cache tồn tại qua các client/process mới; đồng thời trong cùng client chỉ một
  request cùng hash được gửi. Các process khác nhau có thể cùng gọi khi cache miss.
- Cache hit báo prompt_tokens/completion_tokens bằng 0; usage gốc nằm ở original_usage.
- Computed fields như Budget.total được tính lại khi đọc cache.
- `on_attempt(model, attempt, fallback)` có thể là callback sync hoặc async
  để BaseAgent ghi nhật ký, không cần đưa prompt hoặc API key vào log.
