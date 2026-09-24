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
    """Включённая vision без ключа не ходит в сеть и считает изображение годным."""
    def _forbidden(*args, **kwargs):
        raise AssertionError("нет ключа — сетевой вызов недопустим")

    monkeypatch.setattr(vision.requests, "post", _forbidden)

    cfg = FakePhotoCfg(photo_vision_enabled=True, vision_api_key="")
    ok, reason = vision.verify_image(cfg, b"data", "image/jpeg", "кот", "животные")

    assert ok is True
    assert reason == "vision key not set"


def _mock_vision_response(monkeypatch, payload_text):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return FakeResponse(
            status_code=200,
            json_data={"choices": [{"message": {"content": payload_text}}]},
        )

    monkeypatch.setattr(vision.requests, "post", fake_post)
    return captured


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


def test_vision_api_error_does_not_block(monkeypatch):
    """Сбой vision-API не блокирует фото: считаем изображение пригодным."""
    def failing_post(*args, **kwargs):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(vision.requests, "post", failing_post)
    cfg = FakePhotoCfg(photo_vision_enabled=True, vision_api_key="vk")

    ok, reason = vision.verify_image(cfg, b"data", "image/jpeg", "кот", "животные")

    assert ok is True
    assert reason.startswith("vision error")