"""Генерация вопросов через LLM (OpenAI-совместимый Chat Completions API)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

import requests

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — составитель викторин для широкой аудитории. "
    "Ты придумываешь корректные вопросы на общую эрудицию и всегда отвечаешь строго в формате JSON."
)


def _build_user_prompt(topic: str, count: int, language: str, avoid: List[str] | None = None) -> str:
    avoid_block = ""
    if avoid:
        listed = "\n".join(f"- {q}" for q in avoid[:150])
        avoid_block = (
            "\nВАЖНО: не повторяй вопросы, которые уже были раньше. "
            "Ниже список уже заданных вопросов — не используй их и не перефразируй:\n"
            f"{listed}\n"
        )
    return (
        f"Составь ровно {count} вопросов на тему «{topic}» на языке «{language}».\n"
        "Требования:\n"
        "- вопросы из РАЗНЫХ областей (история, география, наука, литература, искусство, "
        "спорт, технологии, языкознание и т.п.), без повторов тем;\n"
        "- все вопросы должны быть разными и не повторять друг друга;\n"
        "- у каждого вопроса ровно 4 варианта ответа и РОВНО ОДИН правильный;\n"
        "- факты должны быть достоверными и однозначными, без спорных формулировок;\n"
        "- варианты должны быть правдоподобными, но только один — верный.\n"
        f"{avoid_block}\n"
        "Верни JSON строго такого вида:\n"
        "{\n"
        '  "questions": [\n'
        "    {\n"
        '      "topic": "История",\n'
        '      "question": "В каком году ... ?",\n'
        '      "options": ["...", "...", "...", "..."],\n'
        "      \"correct_index\": 2\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "Никакого текста кроме JSON."
    )


def extract_json(content: str) -> Dict[str, Any]:
    """Достать JSON из ответа модели, даже если он обёрнут в ```json ... ```."""
    content = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
    if fence:
        content = fence.group(1).strip()
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("В ответе модели не найден JSON")
    return json.loads(content[start : end + 1])


# Совместимый алиас для старого приватного имени.
_extract_json = extract_json


def _validate_questions(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for item in items:
        options = item.get("options") or []
        if len(options) != 4:
            raise ValueError(f"У вопроса должно быть 4 варианта: {item}")
        correct = item.get("correct_index")
        if not isinstance(correct, int) or not (0 <= correct < 4):
            raise ValueError(f"Некорректный correct_index: {item}")
        question = (item.get("question") or "").strip()
        if not question:
            raise ValueError(f"Пустой текст вопроса: {item}")
        result.append(
            {
                "topic": (item.get("topic") or "").strip(),
                "question": question,
                "options": [str(o) for o in options],
                "correct_index": correct,
            }
        )
    return result


def generate_questions(cfg, avoid: List[str] | None = None) -> List[Dict[str, Any]]:
    """Сгенерировать список вопросов через LLM.

    avoid — список ранее заданных вопросов, которые повторять не нужно.
    Возвращает список словарей: topic, question, options[4], correct_index.
    """
    url = f"{cfg.llm_base_url}/chat/completions"
    payload = {
        "model": cfg.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _build_user_prompt(cfg.topic, cfg.count, cfg.language, avoid),
            },
        ],
        "temperature": cfg.llm_temperature,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {cfg.llm_api_key}",
        "Content-Type": "application/json",
    }

    log.info("Запрашиваю %s вопросов у модели %s ...", cfg.count, cfg.llm_model)
    response = requests.post(url, json=payload, headers=headers, timeout=120)

    if response.status_code >= 400:
        # Некоторые провайдеры не поддерживают response_format — пробуем без него.
        log.warning("Ответ %s, повтор без response_format", response.status_code)
        payload.pop("response_format", None)
        response = requests.post(url, json=payload, headers=headers, timeout=120)

    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    data = extract_json(content)
    questions = _validate_questions(data.get("questions", []))

    if len(questions) != cfg.count:
        raise ValueError(
            f"Модель вернула {len(questions)} вопросов вместо {cfg.count}"
        )
    return questions