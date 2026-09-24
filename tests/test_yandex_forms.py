"""Тесты yandex_forms.py: image в payload и multipart-загрузка (сеть замокана)."""

from __future__ import annotations

import pytest

import yandex_forms
from helpers import FakeResponse
from yandex_forms import YandexFormsClient, YandexFormsError


# --- add_enum_question -----------------------------------------------------


def _client_with_captured_request():
    client = YandexFormsClient("token", "org")
    captured = {}

    def fake_request(method, path, payload=None):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return {"id": 777}

    client._request = fake_request  # type: ignore[method-assign]
    return client, captured


def test_add_enum_question_without_image_has_no_image_key():
    """Без `image` ключа `image` в payload нет."""
    client, captured = _client_with_captured_request()

    qid = client.add_enum_question("s1", "Q?", ["a", "b", "c", "d"], 1)

    assert qid == 777
    assert "image" not in captured["payload"]
    assert captured["payload"]["type"] == "enum"


def test_add_enum_question_with_image_includes_it():
    """С `image` payload содержит ровно переданный словарь."""
    client, captured = _client_with_captured_request()
    image = {"id": "img-1", "links": {"original": "https://x/img.jpg"}, "name": "img.jpg"}

    client.add_enum_question("s1", "Q?", ["a", "b", "c", "d"], 0, image=image)

    assert captured["payload"]["image"] == image


def test_add_enum_question_marks_only_correct_item():
    """Правильный вариант один: correct True и scores=1."""
    client, captured = _client_with_captured_request()

    client.add_enum_question("s1", "Q?", ["a", "b", "c", "d"], 2)

    items = captured["payload"]["items"]
    assert [it["correct"] for it in items] == [False, False, True, False]
    assert [it["scores"] for it in items] == [0, 0, 1, 0]


# --- upload_image -----------------------------------------------------------


def _mock_post(monkeypatch, info, status_code=200):
    captured = {}

    def fake_post(url, headers=None, files=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["files"] = files
        return FakeResponse(status_code=status_code, json_data=info)

    monkeypatch.setattr(yandex_forms.requests, "post", fake_post)
    return captured


def test_upload_image_ready_returns_only_public_fields(monkeypatch):
    """check_status=ready: возвращаются только id/links/name, служебные поля убраны."""
    captured = _mock_post(
        monkeypatch,
        {
            "id": "img-1",
            "links": {"original": "https://x/img.jpg"},
            "name": "img.jpg",
            "check_status": "ready",
            "check_mode": "antivirus",
        },
    )
    client = YandexFormsClient("token", "org")

    result = client.upload_image("s1", b"bytes", "img.jpg", "image/jpeg")

    assert set(result.keys()) == {"id", "links", "name"}
    assert result == {
        "id": "img-1",
        "links": {"original": "https://x/img.jpg"},
        "name": "img.jpg",
    }
    assert captured["url"].endswith("/surveys/s1/images")
    assert captured["files"]["image"] == ("img.jpg", b"bytes", "image/jpeg")


def test_upload_image_sends_no_json_content_type(monkeypatch):
    """multipart-запрос НЕ шлёт Content-Type: application/json в заголовках."""
    captured = _mock_post(monkeypatch, {"id": "i", "links": {}, "name": "n", "check_status": "ready"})
    client = YandexFormsClient("token", "org")

    client.upload_image("s1", b"bytes", "img.jpg", "image/jpeg")

    lowered = {k.lower(): v for k, v in captured["headers"].items()}
    assert "content-type" not in lowered
    # авторизация и орг-заголовок при этом сохранены
    assert lowered.get("authorization") == "OAuth token"
    assert lowered.get("x-cloud-org-id") == "org"


@pytest.mark.parametrize("status", ["infected", "error", "deleted"])
def test_upload_image_rejected_status_raises(monkeypatch, status):
    """check_status infected/error/deleted — YandexFormsError."""
    _mock_post(monkeypatch, {"id": "i", "check_status": status})
    client = YandexFormsClient("token", "org")

    with pytest.raises(YandexFormsError) as exc:
        client.upload_image("s1", b"bytes", "img.jpg", "image/jpeg")

    assert status in str(exc.value)


def test_upload_image_http_error_raises(monkeypatch):
    """HTTP >= 400 — YandexFormsError."""
    _mock_post(monkeypatch, {"error": "bad"}, status_code=500)
    client = YandexFormsClient("token", "org")

    with pytest.raises(YandexFormsError):
        client.upload_image("s1", b"bytes", "img.jpg", "image/jpeg")