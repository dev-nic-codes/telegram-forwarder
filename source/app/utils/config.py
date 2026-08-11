from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.utils.paths import DATA_DIR
from app.utils.storage import load_json, save_json

CONFIG_PATH = DATA_DIR / "config.json"


@dataclass
class AppConfig:
    api_id: int
    api_hash: str
    phone: str


def load_config() -> Optional[AppConfig]:
    data = load_json(CONFIG_PATH, default=None)
    if not data:
        return None
    return AppConfig(
        api_id=int(data["api_id"]),
        api_hash=str(data["api_hash"]),
        phone=str(data["phone"]),
    )


def save_config(cfg: AppConfig) -> None:
    save_json(CONFIG_PATH, {"api_id": cfg.api_id, "api_hash": cfg.api_hash, "phone": cfg.phone})
