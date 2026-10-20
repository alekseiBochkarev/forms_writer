"""Состояние приложения: защита от повторной публикации в один день."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict

log = logging.getLogger(__name__)

_STRING_LINE_RE = re.compile(r'^\s*"(?:[^"\\]|\\.)*"\s*,?\s*$')


def _salvage_state(path: str) -> Dict[str, Any]:
    """Восстановить состояние из битого JSON.

    Типичное повреждение — потерянная запятая между элементами списка,
    внесённая ручным слиянием или редактированием. Чиним построчно: если
    строка — законченная строка-элемент без запятой, а следующая тоже
    строка-элемент, вставляем пропущенную запятую. После починки делаем
    резервную копию повреждённого файла.
    """
    with open(path, "r", encoding="utf-8-sig") as f:
        lines = f.readlines()
    fixed = list(lines)
    repaired = 0
    for i in range(len(fixed) - 1):
        cur, nxt = fixed[i], fixed[i + 1]
        if not _STRING_LINE_RE.match(cur.rstrip("\n")):
            continue
        if cur.rstrip("\n").rstrip().endswith(","):
            continue
        if not _STRING_LINE_RE.match(nxt.rstrip("\n")):
            continue
        body = cur.rstrip("\r\n")
        eol = cur[len(body):] or "\n"
        fixed[i] = body + "," + eol
        repaired += 1
    try:
        data = json.loads("".join(fixed))
    except json.JSONDecodeError:
        raise ValueError(
            f"Файл состояния {path} повреждён и не поддаётся автоматическому "
            "восстановлению"
        )
    if not isinstance(data, dict):
        raise ValueError(f"Файл состояния {path} повреждён без возможности восстановления")
    backup = f"{path}.broken"
    with open(backup, "w", encoding="utf-8") as f:
        f.writelines(lines)
    log.warning(
        "Файл состояния %s был повреждён (%s), восстановлен автоматически; "
        "исходная копия сохранена в %s",
        path,
        f"починено строк: {repaired}" if repaired else "структурная правка",
        backup,
    )
    return data


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
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = _salvage_state(path)
    if not isinstance(data, dict):
        raise ValueError(f"Файл состояния {path} повреждён без возможности восстановления")
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
    # фото-поток: заголовки выпусков храним ограниченно (защита от повторов).
    if isinstance(state.get("used_titles"), list):
        state["used_titles"] = state["used_titles"][-100:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")