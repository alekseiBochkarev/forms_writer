"""Тесты llm.py: извлечение/валидация JSON (сеть не задействуется)."""

from __future__ import annotations

import json
import types

import pytest
import requests
from helpers import FakeResponse

from llm import (
    _extract_json,
    _validate_questions,
    extract_json,
    generate_questions,
    post_chat,
)


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


# --- generate_questions: retry/backoff (сеть не задействуется) ---------------


def _cfg(**overrides):
    """Минимальная заглушка конфига для generate_questions/post_chat."""
    base = {
        "llm_api_key": "test-key",
        "llm_base_url": "https://llm.example/v1",
        "llm_model": "test-model",
        "llm_temperature": 0.0,
        "llm_timeout": 120,
        "llm_retries": 1,
        "llm_retry_delay": 0.0,
        "topic": "общая эрудиция",
        "count": 1,
        "language": "ru",
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _ok_response():
    content = json.dumps({"questions": [_item()]}, ensure_ascii=False)
    return FakeResponse(
        200, json_data={"choices": [{"message": {"content": content}}]}
    )


def test_generate_questions_retries_on_read_timeout(monkeypatch):
    """ReadTimeout на первой попытке — запрос повторяется и завершается успешно."""
    calls = {"n": 0}

    def fake_post(url, json, headers, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ReadTimeout("timeout")
        return _ok_response()

    monkeypatch.setattr("llm.requests.post", fake_post)

    questions = generate_questions(_cfg())

    assert calls["n"] == 2
    assert len(questions) == 1


def test_generate_questions_retries_on_5xx(monkeypatch):
    """HTTP 500 — транзиентная ошибка, запрос повторяется."""
    statuses = [500, 200]

    def fake_post(url, json, headers, timeout):
        return _ok_response() if statuses.pop(0) == 200 else FakeResponse(500)

    monkeypatch.setattr("llm.requests.post", fake_post)

    questions = generate_questions(_cfg())

    assert len(questions) == 1


def test_generate_questions_400_uses_fallback_without_response_format(monkeypatch):
    """HTTP 400 не ретраится, а уходит отдельным fallback без response_format."""
    payloads = []

    def fake_post(url, json, headers, timeout):
        payloads.append(dict(json))
        if "response_format" in json:
            return FakeResponse(400, text="bad request")
        return _ok_response()

    monkeypatch.setattr("llm.requests.post", fake_post)

    questions = generate_questions(_cfg())

    assert len(questions) == 1
    assert len(payloads) == 2
    assert "response_format" in payloads[0]
    assert "response_format" not in payloads[1]


def test_fallback_runs_even_with_zero_retries(monkeypatch):
    """LLM_RETRIES=0: 400 на единственной попытке всё равно даёт fallback."""
    payloads = []

    def fake_post(url, json, headers, timeout):
        payloads.append(dict(json))
        if "response_format" in json:
            return FakeResponse(400, text="bad request")
        return _ok_response()

    monkeypatch.setattr("llm.requests.post", fake_post)

    questions = generate_questions(_cfg(llm_retries=0))

    assert len(questions) == 1
    assert len(payloads) == 2
    assert "response_format" not in payloads[1]


def test_fallback_after_transient_then_4xx(monkeypatch):
    """5xx ретраится, затем 4xx уходит в fallback, а не роняет запрос."""
    payloads = []

    def fake_post(url, json, headers, timeout):
        payloads.append(dict(json))
        if len(payloads) == 1:
            return FakeResponse(500)
        if "response_format" in json:
            return FakeResponse(400, text="bad request")
        return _ok_response()

    monkeypatch.setattr("llm.requests.post", fake_post)

    questions = generate_questions(_cfg(llm_retries=1))

    assert len(questions) == 1
    assert len(payloads) == 3
    assert "response_format" not in payloads[2]


def test_403_does_not_use_fallback(monkeypatch):
    """401/403 — не повод слать запрос без response_format."""
    calls = {"n": 0}

    def fake_post(url, json, headers, timeout):
        calls["n"] += 1
        return FakeResponse(403, text="forbidden")

    monkeypatch.setattr("llm.requests.post", fake_post)

    with pytest.raises(RuntimeError):
        generate_questions(_cfg(llm_retries=0))

    assert calls["n"] == 1


def test_error_message_reports_http_code_not_none(monkeypatch):
    """При исчерпании попыток сообщение содержит код, а не None."""

    def fake_post(url, json, headers, timeout):
        return FakeResponse(401, text="unauthorized")

    monkeypatch.setattr("llm.requests.post", fake_post)

    with pytest.raises(RuntimeError) as exc:
        generate_questions(_cfg(llm_retries=0))

    message = str(exc.value)
    assert "None" not in message
    assert "HTTP 401" in message
    assert "после 1 запросов" in message


def test_successful_path_makes_single_request(monkeypatch):
    """Обычный успешный ответ — ровно один запрос, без fallback."""
    calls = {"n": 0}

    def fake_post(url, json, headers, timeout):
        calls["n"] += 1
        return _ok_response()

    monkeypatch.setattr("llm.requests.post", fake_post)

    questions = generate_questions(_cfg())

    assert calls["n"] == 1
    assert len(questions) == 1


def test_post_chat_does_not_mutate_payload(monkeypatch):
    """Fallback не трогает исходный payload вызывающего кода."""
    payload = {"model": "m", "response_format": {"type": "json_object"}}
    snapshot = {"model": "m", "response_format": {"type": "json_object"}}

    def fake_post(url, json, headers, timeout):
        return FakeResponse(400, text="bad request")

    monkeypatch.setattr("llm.requests.post", fake_post)

    with pytest.raises(RuntimeError):
        post_chat(_cfg(llm_retries=0), payload)

    assert payload == snapshot


def test_generate_questions_raises_after_retries_exhausted(monkeypatch):
    """Исчерпание всех попыток — понятная RuntimeError с исходной причиной."""
    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        raise requests.exceptions.ReadTimeout("timeout")

    monkeypatch.setattr("llm.requests.post", fake_post)

    with pytest.raises(RuntimeError) as exc:
        generate_questions(_cfg(llm_retries=2))

    assert calls["n"] == 3
    assert "после 3 запросов" in str(exc.value)
