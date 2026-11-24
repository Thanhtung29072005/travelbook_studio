"""Application settings. Secrets stay on the backend and are redacted in repr."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8-sig", extra="ignore",
    )

    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    frontend_origin: str = "http://localhost:5173"
    max_revisions: int = Field(default=3, ge=0)
    live_food_vnd_per_person_day: int = Field(default=200000, ge=0)
    live_transport_vnd_per_day: int = Field(default=250000, ge=0)
    live_hotel_vnd_per_room_night: int = Field(default=600000, ge=0)
    live_return_travel_vnd_per_person: int = Field(default=2000000, ge=0)
    live_unknown_ticket_vnd_per_person: int = Field(default=50000, ge=0)
    live_contingency_percent: int = Field(default=10, ge=0, le=100)
    budget_economy_hotel_vnd_per_room_night: int = Field(default=400000, ge=0)
    budget_economy_return_vnd_per_person: int = Field(default=1200000, ge=0)
    official_prices_enabled: bool = True
    destination_guide_enabled: bool = True
    destination_guide_timeout_seconds: float = Field(default=45, gt=0, le=90)
    official_prices_max_places: int = Field(default=12, ge=1, le=30)
    official_prices_max_pages: int = Field(default=4, ge=1, le=6)
    official_prices_timeout_seconds: float = Field(default=12, gt=0, le=30)
    official_prices_total_timeout_seconds: float = Field(default=120, gt=0, le=300)
    supervisor_enabled: bool = False
    supervisor_fallback_enabled: bool = True
    nine_router_base_url: str = "http://localhost:20128/v1"
    nine_router_api_key: SecretStr = SecretStr("")
    # Comma-separated: primary first, followed by fallbacks. No invented model IDs.
    nine_router_models: str = ""
    nine_router_image_model: str = ""
    image_style_lock: str = ""
    image_palette: str = "#F3EBDD,#247C82,#C76D4D"
    nine_router_image_fallback_models: str = ""
    image_timeout_seconds: float = Field(default=180, gt=0, le=600)
    image_cache_dir: Path = Path("backend/data/image_cache")
    image_download_hosts: str = "oaidalleapiprodscus.blob.core.windows.net"
    llm_timeout_seconds: float = Field(default=180, gt=0, allow_inf_nan=False)
    llm_max_attempts_per_model: int = Field(default=2, ge=1, le=5)
    llm_retry_delay_seconds: float = Field(default=0.5, ge=0, allow_inf_nan=False)
    llm_structured_output_mode: str = "json_object"
    llm_cache_enabled: bool = True
    llm_cache_ttl_seconds: int = Field(default=86400, gt=0)
    llm_cache_path: Path = Path("backend/data/llm_cache.sqlite3")
    agent_reasoning_enabled: bool = True
    agent_reasoning_required: bool = False
    reviewer_visual_enabled: bool = True
    nine_router_vision_models: str = ""
    reviewer_max_images: int = Field(default=12, ge=1, le=12)
    layout_low_confidence_threshold: float = Field(default=0.7, ge=0, le=1, allow_inf_nan=False)
    data_dir: Path = Path("backend/data")
    data_user_agent: str = Field(default="TravelBookStudio/0.1 (travelbook-studio)", min_length=12)
    data_timeout_seconds: float = Field(default=25, gt=0, le=120)
    data_trust_env: bool = True
    data_cache_path: Path = Path("backend/data/public_api_cache.sqlite3")
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    overpass_fallback_urls: str = "https://overpass.private.coffee/api/interpreter"
    wikidata_entity_base_url: str = "https://www.wikidata.org/wiki/Special:EntityData"
    wikidata_query_url: str = "https://query.wikidata.org/sparql"
    research_wikidata_fallback_enabled: bool = True
    open_meteo_url: str = "https://api.open-meteo.com/v1/forecast"
    nominatim_min_interval_seconds: float = Field(default=1.1, ge=1)
    research_max_places: int = Field(default=12, ge=1, le=30)
    research_radius_m: int = Field(default=15000, ge=100, le=50000)
    research_max_wikidata_requests: int = Field(default=4, ge=0, le=10)
    research_popularity_enabled: bool = True
    research_candidate_limit: int = Field(default=40, ge=12, le=80)
    research_highlight_radius_m: int = Field(default=50000, ge=1000, le=100000)
    research_pageview_limit: int = Field(default=24, ge=0, le=24)
    research_popularity_timeout_seconds: float = Field(default=45, gt=0, le=90)
    osrm_base_url: str = "https://router.project-osrm.org"
    ors_base_url: str = "https://api.openrouteservice.org/v2/matrix"
    openrouteservice_api_key: SecretStr = SecretStr("")
    routing_provider_order: str = "osrm,ors,haversine"
    routing_mode: str = "driving"
    osrm_driving_profile: str = "driving"
    osrm_walking_profile: str = ""
    osrm_cycling_profile: str = ""
    routing_max_points: int = Field(default=40, ge=1, le=50)
    map_basemap_enabled: bool = True
    map_tile_url: str = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
    map_tile_cache_dir: Path = Path('backend/data/map_tile_cache')
    map_tile_max_zoom: int = Field(default=16, ge=2, le=18)
    map_tile_max_count: int = Field(default=16, ge=4, le=25)
    map_network_timeout_seconds: float = Field(default=12, gt=0, le=30)
    map_total_timeout_seconds: float = Field(default=90, gt=0, le=180)
    routing_fallback_speed_kmh: float | None = Field(default=None, gt=0, le=150)
    routing_haversine_factor: float = Field(default=1.35, ge=1, le=3)
    planner_day_start_hour: int = Field(default=8, ge=0, le=22)
    planner_day_end_hour: int = Field(default=21, ge=1, le=23)
    planner_transition_minutes: int = Field(default=10, ge=0, le=60)
    planner_default_visit_minutes: int = Field(default=60, ge=15, le=240)
    planner_max_stops: int = Field(default=6, ge=1, le=10)
    unsplash_access_key: SecretStr = SecretStr("")
    pexels_api_key: SecretStr = SecretStr("")
    wikimedia_api_url: str = "https://commons.wikimedia.org/w/api.php"
    unsplash_base_url: str = "https://api.unsplash.com"
    pexels_base_url: str = "https://api.pexels.com/v1"
    photo_provider_order: str = "wikimedia,unsplash,pexels"
    photo_search_limit: int = Field(default=6, ge=1, le=20)
    photo_min_width: int = Field(default=800, ge=100, le=4000)
    photo_min_height: int = Field(default=450, ge=100, le=4000)
    photo_min_relevance: float = Field(default=0.75, ge=0.1, le=1)
    photo_utm_source: str = Field(default="travelbook_studio", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    reviewer_max_daily_distance_km: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @field_validator("nine_router_base_url", "nominatim_base_url", "overpass_url", "wikidata_entity_base_url", "wikidata_query_url", "open_meteo_url", "osrm_base_url", "ors_base_url", "wikimedia_api_url", "unsplash_base_url", "pexels_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("NINE_ROUTER_BASE_URL must be an HTTP(S) URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("base URL cannot contain credentials, query or fragment")
        return value

    @field_validator("overpass_fallback_urls")
    @classmethod
    def validate_overpass_fallbacks(cls,value):
        items=list(dict.fromkeys(item.strip().rstrip('/') for item in value.split(',') if item.strip()))
        if len(items)>3: raise ValueError('At most three Overpass fallback endpoints')
        return ','.join(cls.validate_base_url(item) for item in items)

    @field_validator("llm_structured_output_mode")
    @classmethod
    def validate_output_mode(cls, value: str) -> str:
        if value not in {"json_object", "json_schema", "prompt"}:
            raise ValueError("output mode must be json_object, json_schema or prompt")
        return value

    @field_validator("photo_provider_order")
    @classmethod
    def validate_photo_order(cls, value):
        items = list(dict.fromkeys(item.strip().lower() for item in value.split(",") if item.strip()))
        if not items or set(items) - {"wikimedia", "unsplash", "pexels"}:
            raise ValueError("Photo providers must be wikimedia, unsplash or pexels")
        return ",".join(items)

    @field_validator("routing_provider_order")
    @classmethod
    def validate_routing_order(cls, value):
        items = list(dict.fromkeys(item.strip().lower() for item in value.split(",") if item.strip()))
        if not items or items[-1] != "haversine" or set(items) - {"osrm", "ors", "haversine"}:
            raise ValueError("Routing chain must use osrm/ors and end with haversine")
        return ",".join(items)

    @field_validator("routing_mode")
    @classmethod
    def validate_routing_mode(cls, value):
        if value not in {"driving", "walking", "cycling"}:
            raise ValueError("Invalid routing mode")
        return value

    @field_validator("osrm_driving_profile", "osrm_walking_profile", "osrm_cycling_profile")
    @classmethod
    def validate_osrm_profile(cls, value):
        import re
        if value and not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("Invalid OSRM profile")
        return value

    @field_validator("data_dir", "llm_cache_path", "data_cache_path", "image_cache_dir")
    @classmethod
    def resolve_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @property
    def models(self) -> list[str]:
        return list(dict.fromkeys(m.strip() for m in self.nine_router_models.split(",") if m.strip()))

    @property
    def database_path(self) -> Path:
        return self.data_dir / "travelbook.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"


@lru_cache
def get_settings() -> Settings:
    return Settings()
