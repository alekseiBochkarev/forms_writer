"""Проверка изображений через vision-модель (OpenAI-совместимый API).

Проверка включена по умолчанию (`PHOTO_VISION_ENABLED=true`) и работает по принципу
fail-closed: если vision-API недоступен, ответ не распознан или модель сообщила о
проблемах, изображение считается непригодным. Так в выпуск не попадают картинки,
где нет нужной сущности (текст, постеры, музейные фото, чужие объекты).
"""

from __future__ import annotations

import base64
import logging
from typing import Tuple

import requests

from llm import extract_json

log = logging.getLogger(__name__)

# Минимальная уверенность модели, при которой считаем изображение подходящим.
CONFIDENCE_THRESHOLD = 0.5

# Правила проверки по классу изображения: кадр фильма / портрет / общий объект.
_KIND_RULES = {
    "film": (
        "Нужен НАСТОЯЩИЙ КАДР (сцена или стоп-кадр) из фильма «{entity}» "
        "(тема: {theme}). Отклоняй, если это постер, афиша, обложка диска или "
        "кассеты, логотип, кадр из другого фильма, фото музейного экспоната, "
        "памятника, афишной тумбы или здания, снимок книги/страницы/статьи, "
        "скриншот, коллаж, мем, рисунок, — а также если узнаваемой сцены этого "
        "фильма на изображении нет."
    ),
    "actor": (
        "Нужен УЗНАВАЕМЫЙ ПОРТРЕТ актёра или актрисы «{entity}» (тема: {theme}), "
        "где хорошо видно лицо именно этого человека. Отклоняй, если это "
        "изображение без лица, неясное или групповое фото, статуя, памятник, "
        "бюст, рисунок, карикатура, шарж, силуэт, постер, афиша, снимок "
        "книги/страницы/документа, скриншот, коллаж, мем, либо на фото другой "
        "человек."
    ),
    "painting": (
        "Нужна РЕПРОДУКЦИЯ живописной картины «{entity}» (тема: {theme}) — "
        "полотно целиком или его узнаваемый фрагмент. Отклоняй портреты и "
        "фотографии самого художника, скульптуры, памятники, здания, интерьеры "
        "музеев, постеры, афиши, снимки книг/страниц/подписей, скриншоты, "
        "коллажи, мемы, — а также если изображено не живописное полотно."
    ),
    "generic": (
        "Нужно ЧЁТКОЕ изображение объекта, персоны или места «{entity}» "
        "(тема: {theme}), которое однозначно узнаётся. Отклоняй, если "
        "изображение состоит в основном из текста (документ, страница, статья, "
        "табличка, надпись, водяной знак, логотип, скриншот), это коллаж или мем, "
        "а также если изображено не то, что заявлено, или объект неразличим."
    ),
}


def _build_instruction(entity: str, theme: str, kind: str) -> str:
    rule = _KIND_RULES.get(kind or "generic", _KIND_RULES["generic"])
    return (
        "Ты строго проверяешь картинки для викторины. "
        + rule.format(entity=entity, theme=theme)
        + " Если сомневаешься — считай изображение неподходящим. "
        "Ответь строго JSON: "
        '{"match": true, "confidence": 0.0, "problems": []}. '
        "match — подходит ли изображение; confidence — уверенность от 0 до 1; "
        "problems — только серьёзные замечания (текст, постер, чужой объект, "
        "несоответствие теме); если изображение годится, оставь список пустым."
    )


def verify_image(
    cfg,
    image_bytes: bytes,
    mime: str,
    entity: str,
    theme: str,
    kind: str = "generic",
) -> Tuple[bool, str]:
    """Проверить, соответствует ли изображение сущности.

    `kind` задаёт класс изображения: ``film`` (нужен кадр фильма), ``actor``
    (нужен портрет человека) или ``generic`` (объект/персона/место).

    Возвращает (ok, reason). Если проверка выключена — (True, "vision disabled").
    При сбое самого vision-API, отсутствии ключа или явном замечании модели
    изображение отклоняется (fail-closed): лучше пропустить кандидата, чем
    опубликовать нерелевантное фото.
    """
    if not cfg.photo_vision_enabled:
        return True, "vision disabled"

    if not cfg.vision_api_key:
        log.warning("PHOTO_VISION_ENABLED включён, но VISION_API_KEY/LLM_API_KEY не задан")
        return False, "vision key not set"

    instruction = _build_instruction(entity, theme, kind)

    encoded = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": cfg.vision_model,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{encoded}"},
                    },
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {cfg.vision_api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            f"{cfg.vision_base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
            timeout=cfg.vision_timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        data = extract_json(content)
    except Exception as exc:  # noqa: BLE001 - сбой проверки отклоняет кандидата
        log.warning(
            "Vision-проверка не удалась (%s), изображение отклонено (fail-closed)", exc
        )
        return False, f"vision error: {exc}"

    match = bool(data.get("match"))
    confidence = data.get("confidence")
    problems = [
        str(p).strip() for p in (data.get("problems") or []) if str(p).strip()
    ]
    reason = "; ".join(problems) or ("ok" if match else "не соответствует")

    if not match:
        log.info("Vision отклонила изображение для «%s»: %s", entity, reason)
        return False, reason

    if isinstance(confidence, (int, float)) and confidence < CONFIDENCE_THRESHOLD:
        reason = f"низкая уверенность {confidence:.2f}: {reason}"
        log.info("Vision отклонила изображение для «%s»: %s", entity, reason)
        return False, reason

    if problems:
        reason = "замечания vision: " + reason
        log.info("Vision отклонила изображение для «%s»: %s", entity, reason)
        return False, reason

    return True, reason