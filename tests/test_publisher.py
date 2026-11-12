"""Тесты publisher.py: вставка вводной в Telegram-анонс (без сети)."""

from __future__ import annotations

import json
import types

import publisher
import pytest


def test_inject_intro_inserts_before_cta_line():
    """Вводная вставляется непосредственно перед строкой со ссылкой."""
    url = "https://forms.yandex.ru/u/abc/"
    text = f"Новый тест: «Имя»\n\nПройти: {url}"

    result = publisher._inject_intro(text, "Это вводная.", url)

    assert result == (
        "Новый тест: «Имя»\n\nЭто вводная.\n\nПройти: "
        f"{url}"
    )


def test_inject_intro_empty_returns_text():
    assert publisher._inject_intro("текст", "", "url") == "текст"


def test_inject_intro_prepends_intro_when_cta_absent():
    """Если ссылки-CTA в тексте нет, вводная ставится в начало (перед текстом)."""
    result = publisher._inject_intro(
        "Текст без ссылки", "Вводная.", "https://forms.yandex.ru/u/abc/"
    )
    assert result == "Вводная.\n\nТекст без ссылки"


def test_publish_announcement_adds_intro_to_telegram(monkeypatch):
    """При announce_intro=true вводная попадает в Telegram-анонс."""
    sent = {}

    def fake_post(token, channel, text):
        sent["text"] = text

    monkeypatch.setattr(publisher, "post_to_telegram", fake_post)

    cfg = types.SimpleNamespace(
        publish_telegram=True,
        publish_vk=False,
        tg_bot_token="t",
        tg_target_channel="@qa_helper_draft",
        announce_intro=True,
        announce_templates=["Анонс «{name}». Пройти: {url}"],
        announce_template="",
    )

    posted = publisher.publish_announcement(
        cfg, "abc", 10, "Имя", intro="Вводная часть."
    )

    assert posted == ["telegram"]
    assert "Вводная часть." in sent["text"]
    # вводная идёт перед CTA
    assert sent["text"].index("Вводная часть.") < sent["text"].index("Пройти:")


def test_publish_announcement_skips_intro_when_disabled(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        publisher, "post_to_telegram", lambda token, channel, text: sent.update(text=text)
    )

    cfg = types.SimpleNamespace(
        publish_telegram=True,
        publish_vk=False,
        tg_bot_token="t",
        tg_target_channel="@c",
        announce_intro=False,
        announce_templates=["Анонс «{name}». Пройти: {url}"],
        announce_template="",
    )

    publisher.publish_announcement(cfg, "abc", 10, "Имя", intro="Вводная часть.")

    assert "Вводная часть." not in sent["text"]


def test_post_to_telegram_reports_api_description(monkeypatch):
    """Ошибка Telegram содержит description и chat_id — иначе причину не понять."""

    class FakeBadResponse:
        status_code = 400
        text = (
            '{"ok":false,"error_code":400,'
            '"description":"Bad Request: chat not found"}'
        )

        def json(self):
            return {
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: chat not found",
            }

    monkeypatch.setattr(publisher.requests, "post", lambda *a, **k: FakeBadResponse())

    with pytest.raises(RuntimeError) as exc:
        publisher.post_to_telegram("token", "@wrong_channel", "текст")

    message = str(exc.value)
    assert "chat not found" in message
    assert "@wrong_channel" in message


def test_publish_announcement_survives_telegram_failure(monkeypatch):
    """Сбой Telegram не роняет запуск и не мешает сохранить состояние."""

    def boom(*args, **kwargs):
        raise RuntimeError("Telegram API error: chat not found")

    monkeypatch.setattr(publisher, "post_to_telegram", boom)

    cfg = types.SimpleNamespace(
        publish_telegram=True,
        publish_vk=False,
        tg_bot_token="t",
        tg_target_channel="@wrong",
        announce_intro=True,
        announce_templates=["Анонс «{name}». Пройти: {url}"],
        announce_template="",
    )

    posted = publisher.publish_announcement(cfg, "abc", 10, "Имя", intro="Вв.")

    assert posted == []


class _FakeResponse:
    """Ответ Bot API с заданным JSON и HTTP-кодом."""

    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = json.dumps(data, ensure_ascii=False) if data is not None else ""

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def test_post_to_telegram_hints_private_channel(monkeypatch):
    """«chat not found» подсказывает про приватный канал и id -100…."""

    def fake_post(*args, **kwargs):
        return _FakeResponse(
            {"ok": False, "description": "Bad Request: chat not found"}, 400
        )

    monkeypatch.setattr(publisher.requests, "post", fake_post)

    with pytest.raises(RuntimeError) as exc:
        publisher.post_to_telegram("token", "@qa_helper_draft", "текст")

    message = str(exc.value)
    assert "chat not found" in message
    assert "-100" in message
    assert "приват" in message.lower()


def test_get_chat_returns_numeric_id_and_title(monkeypatch):
    """getChat отдаёт числовой id канала — он и нужен для приватного канала."""
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        return _FakeResponse(
            {
                "ok": True,
                "result": {
                    "id": -1001234567890,
                    "type": "channel",
                    "title": "QA Helper — drafts",
                },
            }
        )

    monkeypatch.setattr(publisher.requests, "post", fake_post)

    chat = publisher.get_chat("token", "-1001234567890")

    assert chat == {
        "id": -1001234567890,
        "type": "channel",
        "title": "QA Helper — drafts",
        "username": "",
    }
    assert seen["url"].endswith("/getChat")
    assert seen["json"] == {"chat_id": "-1001234567890"}


def test_get_chat_member_returns_status(monkeypatch):
    monkeypatch.setattr(
        publisher.requests,
        "post",
        lambda *a, **k: _FakeResponse({"ok": True, "result": {"status": "administrator"}}),
    )

    assert publisher.get_chat_member("token", "-1001", 42) == "administrator"


def test_list_recent_channels_collects_only_channels(monkeypatch):
    """В кандидаты попадают каналы из forward_from_chat, личные чаты — нет."""
    updates = {
        "ok": True,
        "result": [
            {
                "message": {
                    "chat": {"id": 42, "type": "private"},
                    "forward_from_chat": {
                        "id": -1007777777777,
                        "type": "channel",
                        "title": "QA Helper — drafts",
                    },
                }
            },
            {"message": {"chat": {"id": 42, "type": "private"}}},
            {
                "message": {
                    "chat": {"id": 42, "type": "private"},
                    "forward_origin": {
                        "type": "channel",
                        "chat": {
                            "id": -1009999999999,
                            "type": "channel",
                            "title": "Пересланный канал",
                        },
                    },
                }
            },
            {
                "channel_post": {
                    "chat": {
                        "id": -1008888888888,
                        "type": "channel",
                        "title": "Публичный канал",
                        "username": "public_channel",
                    }
                }
            },
        ],
    }
    monkeypatch.setattr(
        publisher.requests, "post", lambda *a, **k: _FakeResponse(updates)
    )

    channels = publisher.list_recent_channels("token")

    assert channels == [
        {"id": -1007777777777, "title": "QA Helper — drafts", "username": ""},
        {"id": -1009999999999, "title": "Пересланный канал", "username": ""},
        {
            "id": -1008888888888,
            "title": "Публичный канал",
            "username": "public_channel",
        },
    ]


def test_telegram_error_hint_for_missing_rights():
    hint = publisher.telegram_error_hint("Forbidden: bot is not a member")
    assert "администратор" in hint.lower()
