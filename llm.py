"""Генерация вопросов через LLM (OpenAI-совместимый Chat Completions API)."""

from __future__ import annotations

import json
import logging
import re
import time
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


# 4xx, при которых fallback без response_format осмыслен (провайдер его не
# поддерживает / не понимает поле). Для 401/403 fallback не делаем.
FALLBACK_STATUSES = frozenset({400, 404, 415, 422})


def post_chat(cfg, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Выполнить Chat Completions-запрос с ретраями и fallback без response_format.

    Основной цикл повторяет только транзиентные ошибки: таймауты/сетевые сбои,
    HTTP 429 и 5xx (пауза `llm_retry_delay * 2**attempt`). При 4xx из
    ``FALLBACK_STATUSES`` один раз отправляется отдельный запрос без
    ``response_format`` — он НЕ расходует retry-бюджет. Если всё исчерпано —
    RuntimeError с фактическим числом сделанных запросов и кодом ответа.
    Исходный ``payload`` не мутируется.
    """
    url = f"{cfg.llm_base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg.llm_api_key}",
        "Content-Type": "application/json",
    }
    attempts = max(1, cfg.llm_retries + 1)
    request_payload = dict(payload)
    requests_made = 0
    last_error: Exception | None = None
    fallback_payload: Dict[str, Any] | None = None

    for attempt in range(attempts):
        requests_made += 1
        try:
            response = requests.post(
                url, json=request_payload, headers=headers, timeout=cfg.llm_timeout
            )
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < attempts - 1:
                delay = cfg.llm_retry_delay * (2 ** attempt)
                log.warning(
                    "LLM-запрос не удался (%s), повтор через %.1f с", exc, delay
                )
                time.sleep(delay)
                continue
            break

        status = response.status_code
        if status == 429 or status >= 500:
            last_error = requests.HTTPError(f"HTTP {status}", response=response)
            if attempt < attempts - 1:
                delay = cfg.llm_retry_delay * (2 ** attempt)
                log.warning("LLM вернула HTTP %s, повтор через %.1f с", status, delay)
                time.sleep(delay)
                continue
            break

        if status >= 400:
            last_error = requests.HTTPError(f"HTTP {status}", response=response)
            if status in FALLBACK_STATUSES and "response_format" in request_payload:
                fallback_payload = dict(payload)
                fallback_payload.pop("response_format", None)
                log.warning("Ответ %s, повтор без response_format", status)
            break

        return response.json()

    # Fallback отдельным запросом, вне retry-бюджета (ровно один раз).
    if fallback_payload is not None:
        requests_made += 1
        try:
            response = requests.post(
                url, json=fallback_payload, headers=headers, timeout=cfg.llm_timeout
            )
        except requests.exceptions.RequestException as exc:
            last_error = exc
        else:
            if response.status_code < 400:
                return response.json()
            last_error = requests.HTTPError(
                f"HTTP {response.status_code}", response=response
            )

    raise RuntimeError(
        f"LLM-запрос не удался после {requests_made} запросов: {last_error}"
    ) from last_error


def generate_questions(cfg, avoid: List[str] | None = None) -> List[Dict[str, Any]]:
    """Сгенерировать список вопросов через LLM.

    avoid — список ранее заданных вопросов, которые повторять не нужно.
    Возвращает список словарей: topic, question, options[4], correct_index.
    """
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

    log.info("Запрашиваю %s вопросов у модели %s ...", cfg.count, cfg.llm_model)
    data = post_chat(cfg, payload)
    content = data["choices"][0]["message"]["content"]
    questions = _validate_questions(extract_json(content).get("questions", []))

    if len(questions) != cfg.count:
        raise ValueError(
            f"Модель вернула {len(questions)} вопросов вместо {cfg.count}"
        )
    return questions