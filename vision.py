"""Проверка изображений через vision-модель (OpenAI-совместимый API).

Проверка по умолчанию выключена (PHOTO_VISION_ENABLED=false) — тогда функция
сразу возвращает успех и не обращается к сети.
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


def verify_image(cfg, image_bytes: bytes, mime: str, entity: str, theme: str) -> Tuple[bool, str]:
    """Проверить, соответствует ли изображение сущности.

    Возвращает (ok, reason). Если проверка выключена — (True, "vision disabled").
    При сбое самого vision-API считаем изображение пригодным, чтобы не терять
    кандидатов из-за проблем инфраструктуры; решение об отказе принимается только
    по явному ответу модели.
    """
    if not cfg.photo_vision_enabled:
        return True, "vision disabled"

    if not cfg.vision_api_key:
        log.warning("PHOTO_VISION_ENABLED включён, но VISION_API_KEY/LLM_API_KEY не задан")
        return True, "vision key not set"

    instruction = (
        "На изображении должен быть(а) «{entity}» (тема: {theme}). "
        "Проверь, что на фото действительно изображён именно этот объект/персонаж/кадр, "
        "а не похожий. Ответь строго JSON: "
        '{{"match": true, "confidence": 0.0, "problems": []}}. '
        "match — соответствует ли изображение; confidence — уверенность от 0 до 1; "
        "problems — список проблем (пустой, если всё хорошо)."
    ).format(entity=entity, theme=theme)

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
    except Exception as exc:  # noqa: BLE001 - сбой проверки не должен блокировать фото
        log.warning("Vision-проверка не удалась (%s), пропускаю проверку", exc)
        return True, f"vision error: {exc}"

    match = bool(data.get("match"))
    confidence = data.get("confidence")
    problems = data.get("problems") or []
    reason = "; ".join(str(p) for p in problems) or ("ok" if match else "не соответствует")

    if not match:
        log.info("Vision отклонила изображение для «%s»: %s", entity, reason)
        return False, reason

    if isinstance(confidence, (int, float)) and confidence < CONFIDENCE_THRESHOLD:
        reason = f"низкая уверенность {confidence:.2f}: {reason}"
        log.info("Vision отклонила изображение для «%s»: %s", entity, reason)
        return False, reason

    return True, reason