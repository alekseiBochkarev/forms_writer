"""Состояние приложения: защита от повторной публикации в один день."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict


def load_state(path: str) -> Dict[str, Any]:
    default: Dict[str, Any] = {
        "last_publish_date": None,
        "published": [],
        "asked_questions": [],
    }
    if not os.path.exists(path):
        return default
    # utf-8-sig корректно читает файл и с BOM, и без него
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    for key, value in default.items():
        data.setdefault(key, value)
    return data


def save_state(path: str, state: Dict[str, Any]) -> None:
    # храним только последние записи, чтобы файл не разрастался
    if isinstance(state.get("published"), list):
        state["published"] = state["published"][-180:]
    if isinstance(state.get("asked_questions"), list):
        state["asked_questions"] = state["asked_questions"][-500:]
    # фото-поток: ограничиваем списки использованных сущностей по темам
    used = state.get("used_entities")
    if isinstance(used, dict):
        for theme, items in used.items():
            if isinstance(items, list):
                used[theme] = items[-300:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")