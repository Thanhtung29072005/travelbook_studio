from __future__ import annotations

from .config import get_settings
from .db import Database

settings = get_settings()
db = Database(settings.database_path)
