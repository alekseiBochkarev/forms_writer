"""Состояние приложения: защита от повторной публикации в один день."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict


def load_state(path: str) -> Dict[str, Any]:
    default: Dict[str, Any] = {"last_publish_date": None, "published": []}
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for key, value in default.items():
        data.setdefault(key, value)
    return data


def save_state(path: str, state: Dict[str, Any]) -> None:
    # храним только последние записи, чтобы файл не разрастался
    if isinstance(state.get("published"), list):
        state["published"] = state["published"][-180:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")