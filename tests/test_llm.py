"""Тесты llm.py: извлечение/валидация JSON (сеть не задействуется)."""

from __future__ import annotations

import json

import pytest

from llm import _extract_json, _validate_questions, extract_json


# --- extract_json -----------------------------------------------------------


def test_extract_json_pure_json():
    """Чистый JSON-объект разбирается как есть."""
    content = '{"questions": [{"question": "2+2?"}]}'
    assert extract_json(content) == {"questions": [{"question": "2+2?"}]}


def test_extract_json_fenced_json():
    """JSON в обёртке ```json ... ``` извлекается."""
    content = '```json\n{"a": 1, "b": [2, 3]}\n```'
    assert extract_json(content) == {"a": 1, "b": [2, 3]}


def test_extract_json_fenced_without_language():
    """Обёртка ``` без пометки языка тоже поддерживается."""
    content = '```\n{"ok": true}\n```'
    assert extract_json(content) == {"ok": True}


def test_extract_json_with_surrounding_text():
    """Лишний текст до/после JSON игнорируется (берётся первая { и последняя })."""
    content = 'Вот ответ модели:\n{"a": 1}\nНадеюсь, помог!'
    assert extract_json(content) == {"a": 1}


def test_extract_json_missing_raises():
    """Если JSON в ответе нет — ValueError."""
    with pytest.raises(ValueError):
        extract_json("никакого json здесь нет")


def test_extract_json_alias_points_to_same_function():
    """Исторический алиас _extract_json ссылается на ту же функцию."""
    assert _extract_json is extract_json


def test_extract_json_invalid_payload_raises():
    """Сломанный JSON (скобки есть, содержимое битое) — ошибка парсинга."""
    with pytest.raises(json.JSONDecodeError):
        extract_json('{"a": }')


# --- _validate_questions ----------------------------------------------------


def _item(**overrides):
    base = {
        "topic": "История",
        "question": "В каком году?",
        "options": ["1", "2", "3", "4"],
        "correct_index": 2,
    }
    base.update(overrides)
    return base


def test_validate_questions_accepts_four_options():
    """Корректный вопрос проходит и нормализуется."""
    result = _validate_questions([_item()])

    assert len(result) == 1
    assert result[0]["options"] == ["1", "2", "3", "4"]
    assert result[0]["correct_index"] == 2
    assert result[0]["topic"] == "История"


def test_validate_questions_correct_index_bounds():
    """correct_index обязан лежать в [0, 3] и быть int."""
    for bad in (4, -1, 100):
        with pytest.raises(ValueError):
            _validate_questions([_item(correct_index=bad)])

    for bad in ("1", 1.5, None):
        with pytest.raises(ValueError):
            _validate_questions([_item(correct_index=bad)])


def test_validate_questions_requires_exactly_four_options():
    """Ровно 4 варианта: 3 и 5 — ошибка."""
    for bad in (["1", "2", "3"], ["1", "2", "3", "4", "5"]):
        with pytest.raises(ValueError):
            _validate_questions([_item(options=bad)])


def test_validate_questions_rejects_empty_question():
    """Пустой/пробельный вопрос — ошибка."""
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            _validate_questions([_item(question=bad)])