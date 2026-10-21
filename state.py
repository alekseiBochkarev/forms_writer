"""Состояние приложения: защита от повторной публикации в один день."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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


def now_utc_iso() -> str:
    """Текущий момент в ISO-формате (UTC) — для publish_ts в записях published."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_publish(
    state: Dict[str, Any],
    survey_id: str = "",
    url: str = "",
    date: Optional[str] = None,
    rubric_id: Optional[str] = None,
    series_no: Optional[int] = None,
    threshold: Optional[int] = None,
    count: Optional[int] = None,
    publish_ts: Optional[str] = None,
    repack_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Добавить запись о публикации в published и вернуть её.

    Поля rubric_id/series_no/threshold/count/publish_ts/repack_of опциональны:
    старые state-файлы без них читаются как раньше, а старый код игнорирует
    новые поля.
    """
    entry: Dict[str, Any] = {
        "survey_id": survey_id,
        "date": date or today_utc(),
    }
    if url:
        entry["url"] = url
    if rubric_id is not None:
        entry["rubric_id"] = rubric_id
    if series_no is not None:
        entry["series_no"] = series_no
    if threshold is not None:
        entry["threshold"] = threshold
    if count is not None:
        entry["count"] = count
    entry["publish_ts"] = publish_ts or now_utc_iso()
    if repack_of is not None:
        entry["repack_of"] = repack_of
    if not isinstance(state.get("published"), list):
        state["published"] = []
    state["published"].append(entry)
    return entry


def _rubric_entries(state: Dict[str, Any], rubric_id: str) -> List[Dict[str, Any]]:
    """Записи published, относящиеся к рубрике (без rubric_id — не рубричные)."""
    published = state.get("published")
    if not isinstance(published, list):
        return []
    return [
        entry
        for entry in published
        if isinstance(entry, dict) and entry.get("rubric_id") == rubric_id
    ]


def last_publish_date_rubric(state: Dict[str, Any], rubric_id: str) -> Optional[str]:
    """Дата (ISO-день) последней публикации рубрики, если она есть.

    Берётся из publish_ts последней записи рубрики с корректной меткой;
    для записей без publish_ts используется их поле date.
    """
    result: Optional[str] = None
    for entry in _rubric_entries(state, rubric_id):
        ts = str(entry.get("publish_ts") or entry.get("date") or "")
        if len(ts) >= 10:
            day = ts[:10]
            if result is None or day >= result:
                result = day
    return result


def rubric_entries(state: Dict[str, Any], rubric_id: str) -> List[Dict[str, Any]]:
    """Публичный доступ к записям published рубрики (см. `_rubric_entries`)."""
    return _rubric_entries(state, rubric_id)


def next_series_no(state: Dict[str, Any], rubric_id: str) -> int:
    """Следующий номер выпуска рубрики: max(series_no)+1, старт с 1.

    Репак-потомки (записи с `repack_of`) не учитываются: репак — перевыпуск той
    же серии с тем же `series_no`, он не должен сдвигать счётчик выпусков.
    """
    numbers = [
        entry["series_no"]
        for entry in _rubric_entries(state, rubric_id)
        if isinstance(entry.get("series_no"), int) and not entry.get("repack_of")
    ]
    return max(numbers) + 1 if numbers else 1


def allow_publish_rubric(
    state: Dict[str, Any], rubric_id: str, force: bool = False
) -> bool:
    """Можно ли публиковать рубрику: не чаще раза в UTC-день (force — всегда)."""
    if force:
        return True
    return last_publish_date_rubric(state, rubric_id) != today_utc()