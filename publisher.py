"""Публикация анонса формы в Telegram и VK."""

from __future__ import annotations

import logging
import random
from datetime import date
from typing import List

import requests

log = logging.getLogger(__name__)

# Формулировки анонса по умолчанию. При каждом запуске выбирается случайная,
# поэтому посты в Telegram и VK слегка отличаются.
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


def post_to_telegram(token: str, channel: str, text: str) -> None:
    """Отправить сообщение в Telegram-канал через Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": channel, "text": text}
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")


def post_to_vk(token: str, group_id: str, text: str) -> None:
    """Опубликовать пост на стене VK-группы от её имени."""
    url = "https://api.vk.com/method/wall.post"
    params = {
        "access_token": token,
        "v": "5.199",
        "owner_id": f"-{group_id}",
        "from_group": 1,
        "message": text,
    }
    response = requests.post(url, data=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(f"VK API error: {data['error']}")


def publish_announcement(cfg, survey_id: str, count: int, name: str) -> List[str]:
    """Опубликовать анонс во все настроенные соцсети.

    Возвращает список каналов, куда публикация удалась.
    """
    text = build_announcement(cfg, survey_id, count, name)
    posted: List[str] = []

    if cfg.publish_telegram:
        if cfg.tg_bot_token and cfg.tg_target_channel:
            post_to_telegram(cfg.tg_bot_token, cfg.tg_target_channel, text)
            posted.append("telegram")
            log.info("Анонс опубликован в Telegram")
        else:
            log.info("Пропускаю Telegram: не заданы TG_BOT_TOKEN / TG_TARGET_CHANNEL")

    if cfg.publish_vk:
        if cfg.vk_access_token and cfg.vk_group_id:
            post_to_vk(cfg.vk_access_token, cfg.vk_group_id, text)
            posted.append("vk")
            log.info("Анонс опубликован в VK")
        else:
            log.info("Пропускаю VK: не заданы VK_ACCESS_TOKEN / VK_GROUP_ID")

    return posted