"""Тесты vision.py: выключенная проверка не ходит в сеть; мок-ответы обрабатываются."""

from __future__ import annotations

import pytest

import vision
from helpers import FakePhotoCfg, FakeResponse


def test_vision_disabled_makes_no_network_call(monkeypatch):
    """PHOTO_VISION_ENABLED=false — сразу успех и НИ ОДНОГО сетевого вызова."""
    def _forbidden(*args, **kwargs):
        raise AssertionError("vision выключена, сетевой вызов недопустим")

    monkeypatch.setattr(vision.requests, "post", _forbidden)

    cfg = FakePhotoCfg(photo_vision_enabled=False)
    ok, reason = vision.verify_image(cfg, b"\xff\xd8\xff", "image/jpeg", "кот", "животные")

    assert ok is True
    assert reason == "vision disabled"


def test_vision_enabled_without_key_makes_no_network_call(monkeypatch):
    """Включённая vision без ключа не ходит в сеть и отклоняет кандидата (fail-closed)."""
    def _forbidden(*args, **kwargs):
        raise AssertionError("нет ключа — сетевой вызов недопустим")

    monkeypatch.setattr(vision.requests, "post", _forbidden)

    cfg = FakePhotoCfg(photo_vision_enabled=True, vision_api_key="")
    ok, reason = vision.verify_image(cfg, b"data", "image/jpeg", "кот", "животные")

    assert ok is False
    assert reason == "vision key not set"


def _mock_vision_response(monkeypatch, payload_text):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResponse(
            status_code=200,
            json_data={"choices": [{"message": {"content": payload_text}}]},
        )

    monkeypatch.setattr(vision.requests, "post", fake_post)
    return captured


def _instruction_of(captured):
    """Текст инструкции из отправленного vision-запроса."""
    content = captured["json"]["messages"][0]["content"]
    return next(part["text"] for part in content if part["type"] == "text")


def test_vision_match_accepted(monkeypatch):
    """Явный match=true с высокой уверенностью — изображение принято."""
    captured = _mock_vision_response(
        monkeypatch, '{"match": true, "confidence": 0.9, "problems": []}'
    )
    cfg = FakePhotoCfg(photo_vision_enabled=True, vision_api_key="vk")

    ok, reason = vision.verify_image(cfg, b"data", "image/png", "кот", "животные")

    assert ok is True
    assert reason == "ok"
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer vk"


def test_vision_no_match_rejected(monkeypatch):
    """match=false — изображение отклонено с причиной."""
    _mock_vision_response(
        monkeypatch,
        '{"match": false, "confidence": 0.9, "problems": ["похожий объект"]}',
    )
    cfg = FakePhotoCfg(photo_vision_enabled=True, vision_api_key="vk")

    ok, reason = vision.verify_image(cfg, b"data", "image/jpeg", "кот", "животные")

    assert ok is False
    assert "похожий объект" in reason


def test_vision_low_confidence_rejected(monkeypatch):
    """match=true, но уверенность ниже порога — отклонено."""
    _mock_vision_response(
        monkeypatch, '{"match": true, "confidence": 0.2, "problems": []}'
    )
    cfg = FakePhotoCfg(photo_vision_enabled=True, vision_api_key="vk")

    ok, reason = vision.verify_image(cfg, b"data", "image/jpeg", "кот", "животные")

    assert ok is False
    assert "низкая уверенность" in reason


def test_vision_api_error_rejects_candidate(monkeypatch):
    """Сбой vision-API отклоняет кандидата (fail-closed), а не пропускает его."""
    def failing_post(*args, **kwargs):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(vision.requests, "post", failing_post)
    cfg = FakePhotoCfg(photo_vision_enabled=True, vision_api_key="vk")

    ok, reason = vision.verify_image(cfg, b"data", "image/jpeg", "кот", "животные")

    assert ok is False
    assert reason.startswith("vision error")


def test_vision_match_with_problems_rejected(monkeypatch):
    """match=true, но есть замечания (например текст на фото) — отклоняем."""
    _mock_vision_response(
        monkeypatch,
        '{"match": true, "confidence": 0.9, "problems": ["на фото в основном текст"]}',
    )
    cfg = FakePhotoCfg(photo_vision_enabled=True, vision_api_key="vk")

    ok, reason = vision.verify_image(cfg, b"data", "image/jpeg", "кот", "животные")

    assert ok is False
    assert "текст" in reason


def test_vision_film_kind_forbids_posters(monkeypatch):
    """Для фильмов инструкция требует кадр и прямо запрещает постеры/афиши."""
    captured = _mock_vision_response(
        monkeypatch, '{"match": true, "confidence": 0.9, "problems": []}'
    )
    cfg = FakePhotoCfg(photo_vision_enabled=True, vision_api_key="vk")

    vision.verify_image(
        cfg, b"data", "image/jpeg", "Иван Васильевич", "советские фильмы", "film"
    )

    text = _instruction_of(captured).lower()
    assert "кадр" in text
    assert "постер" in text
    assert "афиша" in text


def test_vision_actor_kind_forbids_non_portraits(monkeypatch):
    """Для актёров инструкция требует портрет и запрещает статуи/рисунки/афиши."""
    captured = _mock_vision_response(
        monkeypatch, '{"match": true, "confidence": 0.9, "problems": []}'
    )
    cfg = FakePhotoCfg(photo_vision_enabled=True, vision_api_key="vk")

    vision.verify_image(
        cfg, b"data", "image/jpeg", "Нонна Мордюкова", "советские актрисы", "actor"
    )

    text = _instruction_of(captured).lower()
    assert "портрет" in text
    assert "статуя" in text
    assert "афиша" in text


def test_vision_generic_kind_forbids_text(monkeypatch):
    """Для общих тем инструкция запрещает изображения из одного текста."""
    captured = _mock_vision_response(
        monkeypatch, '{"match": true, "confidence": 0.9, "problems": []}'
    )
    cfg = FakePhotoCfg(photo_vision_enabled=True, vision_api_key="vk")

    vision.verify_image(cfg, b"data", "image/jpeg", "синий кит", "животные", "generic")

    text = _instruction_of(captured).lower()
    assert "текст" in text
    assert "синий кит" in text