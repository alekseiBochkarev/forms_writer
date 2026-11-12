"""Публикация анонса формы в Telegram."""

from __future__ import annotations

import logging
import random
from datetime import date
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger(__name__)

# Формулировки анонса по умолчанию. При каждом запуске выбирается случайная,
# поэтому посты в канале слегка отличаются.
DEFAULT_ANNOUNCE_TEMPLATES = [
    "Новый тест: «{name}»\n\n{count} вопросов на разные темы — проверьте свой кругозор.\n\nПройти: {url}",
    "Проверьте эрудицию: «{name}»\n\n{count} вопросов из разных областей. Справитесь?\n\nПройти: {url}",
    "Свежий тест: «{name}»\n\n{count} вопросов на общую эрудицию — узнайте, насколько широк ваш кругозор.\n\nПройти: {url}",
    "«{name}» — новый тест дня\n\n{count} вопросов из разных сфер. Ответьте и узнайте результат.\n\nПройти: {url}",
    "Готов новый тест: «{name}»\n\n{count} вопросов на разные темы. Проверьте себя за пару минут.\n\nПройти: {url}",
    "Тест на кругозор: «{name}»\n\n{count} вопросов из разных областей знаний. Какой результат у вас?\n\nПройти: {url}",
    "Небольшая разминка для ума: «{name}»\n\n{count} вопросов на разные темы — сравните результат с друзьями.\n\nПройти: {url}",
]


def _effective_templates(cfg) -> List[str]:
    templates = getattr(cfg, "announce_templates", None)
    if templates:
        return templates
    single = (getattr(cfg, "announce_template", "") or "").strip()
    if single:
        return [single]
    return DEFAULT_ANNOUNCE_TEMPLATES


def build_announcement(cfg, survey_id: str, count: int, name: str) -> str:
    """Собрать текст анонса со ссылкой на форму (случайная формулировка)."""
    url = f"https://forms.yandex.ru/u/{survey_id}/"
    template = random.choice(_effective_templates(cfg))
    return template.format(name=name, count=count, url=url, date=date.today().isoformat())


def _inject_intro(text: str, intro: str, url: str) -> str:
    """Вставить вводную перед строкой с CTA (ссылкой на форму)."""
    intro = (intro or "").strip()
    if not intro:
        return text
    position = text.rfind(url)
    if position == -1:
        return f"{intro}\n\n{text}"
    line_start = text.rfind("\n", 0, position)
    line_start = 0 if line_start == -1 else line_start + 1
    return f"{text[:line_start].rstrip()}\n\n{intro}\n\n{text[line_start:]}"


def _telegram_call(
    token: str, method: str, params: Optional[Dict[str, Any]] = None
) -> Any:
    """Вызвать метод Bot API и вернуть поле `result`.

    Ошибки Telegram приходят с HTTP 200 и `ok: false`, поэтому проверяем оба
    признака. В тексте ошибки оставляем `description` — без него причину
    (неверный канал, бот не админ) не понять.
    """
    url = f"https://api.telegram.org/bot{token}/{method}"
    response = requests.post(url, json=params or {}, timeout=30)
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code >= 400 or not (data or {}).get("ok"):
        description = (data or {}).get("description")
        detail = description or (response.text or "")[:500]
        raise RuntimeError(
            f"{method}: HTTP {response.status_code}: {detail}"
        )
    return (data or {}).get("result")


def telegram_error_hint(error: str) -> str:
    """Подсказка к частым ошибкам публикации в канал.

    Отдельно про приватные каналы: у них нет `@username`, Bot API принимает
    только числовой id вида `-100…`, а бот должен быть админом канала.
    """
    text = (error or "").lower()
    if "chat not found" in text:
        return (
            " — Telegram не знает такой chat_id. Если канал приватный, у него "
            "нет @username: укажите числовой id вида -100… (переменная "
            "NEW_TESTS_TG_TARGET_CHANNEL) и добавьте бота админом канала."
        )
    if (
        "bot is not a member" in text
        or "not enough rights" in text
        or "have no rights" in text
        or "chat_write_forbidden" in text
    ):
        return (
            " — добавьте бота администратором канала с правом «Публикация "
            "сообщений»."
        )
    if "unauthorized" in text:
        return " — проверьте токен бота (TG_BOT_TOKEN / NEW_TESTS_TG_BOT_TOKEN)."
    return ""


def post_to_telegram(token: str, channel: str, text: str) -> None:
    """Отправить сообщение в Telegram-канал через Bot API."""
    try:
        _telegram_call(token, "sendMessage", {"chat_id": channel, "text": text})
    except RuntimeError as exc:
        raise RuntimeError(
            f"Telegram API error (chat_id={channel!r}): {exc}"
            f"{telegram_error_hint(str(exc))}"
        ) from exc


def get_me(token: str) -> Dict[str, Any]:
    """Информация о боте-владельце токена (`getMe`)."""
    return _telegram_call(token, "getMe") or {}


def get_chat(token: str, channel: str) -> Dict[str, Any]:
    """Разрешить chat_id и вернуть базовые поля канала (`getChat`)."""
    result = _telegram_call(token, "getChat", {"chat_id": channel}) or {}
    return {
        "id": result.get("id"),
        "type": result.get("type") or "",
        "title": result.get("title") or result.get("username") or "",
        "username": result.get("username") or "",
    }


def get_chat_member(token: str, channel: str, user_id: Any) -> str:
    """Статус бота в канале: creator / administrator / member / …"""
    result = _telegram_call(
        token, "getChatMember", {"chat_id": channel, "user_id": user_id}
    ) or {}
    return result.get("status") or ""


def list_recent_channels(token: str) -> List[Dict[str, Any]]:
    """Каналы из последних обновлений бота — чтобы найти id приватного канала.

    Достаточно переслать любой пост канала боту в личку: в обновлении придёт
    `forward_from_chat` с числовым id (`-100…`).
    """
    updates = _telegram_call(
        token,
        "getUpdates",
        {
            "limit": 100,
            "allowed_updates": [
                "message",
                "channel_post",
                "edited_message",
                "edited_channel_post",
            ],
        },
    ) or []
    found: Dict[Any, Dict[str, Any]] = {}
    for update in updates:
        if not isinstance(update, dict):
            continue
        for key in ("message", "channel_post", "edited_message", "edited_channel_post"):
            payload = update.get(key)
            if not isinstance(payload, dict):
                continue
            candidates = [payload.get("chat"), payload.get("forward_from_chat")]
            # Bot API 7.0+ присылает пересланное как forward_origin.chat,
            # старое поле forward_from_chat оставлено для совместимости.
            origin = payload.get("forward_origin")
            if isinstance(origin, dict):
                candidates.append(origin.get("chat"))
            for chat in candidates:
                if isinstance(chat, dict) and chat.get("type") == "channel":
                    if chat.get("id") is not None:
                        found[chat["id"]] = {
                            "id": chat["id"],
                            "title": chat.get("title") or "",
                            "username": chat.get("username") or "",
                        }
    return list(found.values())


def publish_announcement(
    cfg, survey_id: str, count: int, name: str, intro: str = ""
) -> List[str]:
    """Опубликовать анонс в Telegram.

    Вводная (если задана и включён `announce_intro`) добавляется в текст
    анонса перед CTA. Возвращает список каналов, куда публикация удалась.
    Сбой публикации не роняет запуск: форма к этому моменту уже создана и
    опубликована, поэтому ошибка только логируется, а состояние всё равно
    сохраняется.
    """
    text = build_announcement(cfg, survey_id, count, name)
    posted: List[str] = []

    if cfg.publish_telegram:
        if cfg.tg_bot_token and cfg.tg_target_channel:
            url = f"https://forms.yandex.ru/u/{survey_id}/"
            tg_text = text
            if getattr(cfg, "announce_intro", True):
                tg_text = _inject_intro(text, intro, url)
            try:
                post_to_telegram(cfg.tg_bot_token, cfg.tg_target_channel, tg_text)
                posted.append("telegram")
                log.info("Анонс опубликован в Telegram")
            except Exception as exc:  # noqa: BLE001 - форма уже опубликована
                log.error("Не удалось опубликовать анонс в Telegram: %s", exc)
        else:
            log.info("Пропускаю Telegram: не заданы TG_BOT_TOKEN / TG_TARGET_CHANNEL")

    return posted