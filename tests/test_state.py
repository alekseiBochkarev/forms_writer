"""Тесты state.py: загрузка/сохранение и обрезка накопительных списков."""

from __future__ import annotations

import json
from pathlib import Path

from state import load_state, save_state, today_utc


def test_load_state_missing_file_returns_defaults(tmp_path: Path):
    """Для отсутствующего файла возвращается полный набор ключей по умолчанию."""
    path = tmp_path / "state.json"
    state = load_state(str(path))

    assert state == {
        "last_publish_date": None,
        "published": [],
        "asked_questions": [],
    }
    assert not path.exists()


def test_load_state_fills_missing_keys(tmp_path: Path):
    """Недостающие ключи дописываются значениями по умолчанию."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"published": [{"survey_id": "1"}]}), encoding="utf-8")

    state = load_state(str(path))

    assert state["published"] == [{"survey_id": "1"}]
    assert state["last_publish_date"] is None
    assert state["asked_questions"] == []


def test_load_state_reads_utf8_with_bom(tmp_path: Path):
    """Файл с BOM читается корректно (Windows-совместимость)."""
    path = tmp_path / "state.json"
    payload = {"last_publish_date": "2026-01-01", "asked_questions": ["Вопрос"]}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    state = load_state(str(path))

    assert state["last_publish_date"] == "2026-01-01"
    assert state["asked_questions"] == ["Вопрос"]


def test_save_state_round_trip(tmp_path: Path):
    """save_state/load_state сохраняют данные и пишут читаемый UTF-8."""
    path = tmp_path / "state.json"
    original = {
        "last_publish_date": "2026-09-24",
        "published": [{"survey_id": "abc", "url": "https://forms.yandex.ru/u/abc/"}],
        "asked_questions": ["Сколько струн у гитары?"],
    }

    save_state(str(path), original)
    restored = load_state(str(path))

    assert restored == original
    # файл действительно UTF-8 без экранирования кириллицы
    raw = path.read_text(encoding="utf-8")
    assert "Сколько струн" in raw


def test_save_state_truncates_published_to_last_180(tmp_path: Path):
    """published хранит только последние 180 записей (не разрастается)."""
    path = tmp_path / "state.json"
    published = [{"n": i} for i in range(200)]

    save_state(str(path), {"published": published})

    saved = load_state(str(path))
    assert len(saved["published"]) == 180
    assert saved["published"][0] == {"n": 20}
    assert saved["published"][-1] == {"n": 199}


def test_save_state_truncates_used_entities_per_theme(tmp_path: Path):
    """used_entities (dict) обрезается по каждой теме до 300 элементов."""
    path = tmp_path / "state.json"
    state = {
        "used_entities": {
            "животные": [f"e{i}" for i in range(350)],
            "растения": ["роза"],
            "битая": "не список",
        }
    }

    save_state(str(path), state)

    saved = load_state(str(path))
    assert len(saved["used_entities"]["животные"]) == 300
    assert saved["used_entities"]["животные"][0] == "e50"
    assert saved["used_entities"]["животные"][-1] == "e349"
    # короткий список и не-список не трогаются
    assert saved["used_entities"]["растения"] == ["роза"]
    assert saved["used_entities"]["битая"] == "не список"


def test_save_state_keeps_erudition_keys_intact(tmp_path: Path):
    """Обрезка фото-полей не затрагивает эрудиционные ключи состояния."""
    path = tmp_path / "state.json"
    state = {
        "last_publish_date": "2026-09-24",
        "asked_questions": ["a", "b", "c"],
        "published": [{"n": 1}],
        "used_entities": {"животные": [f"e{i}" for i in range(400)]},
        "custom_key": {"x": 1},
    }

    save_state(str(path), state)

    saved = load_state(str(path))
    assert saved["last_publish_date"] == "2026-09-24"
    assert saved["asked_questions"] == ["a", "b", "c"]
    assert saved["published"] == [{"n": 1}]
    assert saved["custom_key"] == {"x": 1}


def test_save_state_caps_asked_questions(tmp_path: Path):
    """save_state ограничивает и asked_questions, чтобы файл не разрастался.

    Основной поток уже сам режет список до 500 (main.py, photo_flow.py), но
    защита на уровне сохранения ожидается здесь же — рядом с обрезкой published
    и used_entities, иначе любой вызывающий код может записать неограниченный
    список.
    """
    path = tmp_path / "state.json"
    asked = [f"Вопрос {i}" for i in range(1000)]

    save_state(str(path), {"asked_questions": asked})

    saved = load_state(str(path))
    assert len(saved["asked_questions"]) <= 500
    assert saved["asked_questions"] == asked[-500:]


def test_today_utc_format():
    """today_utc возвращает ISO-дату в UTC."""
    value = today_utc()
    assert len(value) == 10
    assert value[4] == "-" and value[7] == "-"