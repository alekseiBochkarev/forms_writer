"""Тесты publisher.py: вставка вводной в Telegram-анонс (без сети)."""

from __future__ import annotations

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