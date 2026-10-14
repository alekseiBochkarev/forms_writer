"""Отдельный LLM-проход факт-чекинга вопросов перед публикацией.

Проверяет каждый вопрос: действительно ли указанный правильный ответ верен и
однозначен. Вопросы, признанные недостоверными/двусмысленными, отбрасываются,
чтобы модель не публиковала ошибочные факты (особенно в тематических выпусках о
творческих личностях).

При сбое LLM-проверки вопросы НЕ отбрасываются (шаг считается необязательной
страховкой): в лог пишется предупреждение, выпуск не блокируется.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from llm import extract_json, post_chat

log = logging.getLogger(__name__)

FACTCHECK_SYSTEM = (
    "Ты — строгий фактчекер викторин. Ты проверяешь, действительно ли указанный "
    "правильный ответ верен и однозначен, и всегда отвечаешь строго в формате JSON."
)


def _answer_of(question: Dict[str, Any]) -> str:
    options = question.get("options") or []
    index = question.get("correct_index")
    if isinstance(index, int) and 0 <= index < len(options):
        return str(options[index])
    return ""


def _build_prompt(questions: List[Dict[str, Any]]) -> str:
    lines = []
    for i, question in enumerate(questions):
        lines.append(
            f"{i}. Вопрос: {question.get('question', '')}\n"
            f"   Указан верный ответ: {_answer_of(question)}"
        )
    block = "\n".join(lines)
    return (
        "Проверь каждый вопрос викторины: верен и однозначен ли указанный ответ.\n"
        "Считай вопрос НЕ прошедшим, если: указанный ответ фактически неверен; "
        "вопрос допускает другой правильный ответ; формулировка двусмысленная "
        "или основана на спорном/непроверяемом факте.\n\n"
        f"{block}\n\n"
        "Верни JSON строго такого вида:\n"
        '{"results": [{"index": 0, "ok": true, "reason": ""}]}\n'
        "Для каждого вопроса — один элемент: index (номер из списка), ok "
        "(прошёл ли), reason (коротко на русском, если ok=false; иначе пусто). "
        "Никакого текста кроме JSON."
    )


def _llm_check(cfg, questions: List[Dict[str, Any]]) -> Dict[str, Any]:
    payload = {
        "model": cfg.effective_factcheck_model(),
        "messages": [
            {"role": "system", "content": FACTCHECK_SYSTEM},
            {"role": "user", "content": _build_prompt(questions)},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    data = post_chat(cfg, payload)
    content = data["choices"][0]["message"]["content"]
    return extract_json(content)


def filter_questions(
    cfg, questions: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Вернуть только вопросы, прошедшие факт-чек.

    При выключенном `cfg.factcheck_enabled` или сбое LLM-проверки возвращается
    тот же список без изменений (не блокируем выпуск).
    """
    if not getattr(cfg, "factcheck_enabled", True) or not questions:
        return list(questions)

    try:
        data = _llm_check(cfg, questions)
    except Exception as exc:  # noqa: BLE001 - факт-чек необязательная страховка
        log.warning(
            "Факт-чек не выполнен (%s) — вопросы оставлены без изменений", exc
        )
        return list(questions)

    if not isinstance(data, dict):
        log.warning("Факт-чек вернул неожиданный формат — вопросы оставлены")
        return list(questions)

    bad: Dict[int, str] = {}
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int) or not 0 <= index < len(questions):
            if isinstance(index, int):
                log.warning("Факт-чек вернул индекс вне диапазона: %s", index)
            continue
        if item.get("ok") is False:
            bad[index] = str(item.get("reason") or "").strip()

    kept: List[Dict[str, Any]] = []
    dropped = 0
    for i, question in enumerate(questions):
        if i in bad:
            dropped += 1
            log.info(
                "  факт-чек отклонил вопрос: %s (%s)",
                question.get("question", ""),
                bad[i] or "без пояснения",
            )
            continue
        kept.append(question)
    if dropped:
        log.info("Факт-чек: отклонено вопросов %s из %s", dropped, len(questions))
    return kept