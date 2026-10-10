"""Отдельный LLM-проход ревью перед публикацией.

Проверяет вводную/заключительную части: спойлеры (слова из правильных ответов),
соответствие анонса фактическим темам, отсутствие шаблонности и повторов с
прошлыми выпусками, наличие инструкции по ответам и CTA. Возвращает результат
`ok`/issues. Дополнительно есть детерминированная проверка заголовков на
спойлер (без обращения к LLM).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from llm import extract_json, post_chat

log = logging.getLogger(__name__)

REVIEW_SYSTEM = (
    "Ты — строгий редактор викторин. Ты проверяешь вводную и заключительную "
    "части теста перед публикацией и всегда отвечаешь строго в формате JSON."
)

# Слова длиной от 4 букв: короткие предлоги/союзы и числа не считаем
# значимыми при проверке на спойлер.
_WORD_RE = re.compile(r"[а-яёa-z]{4,}", re.IGNORECASE)


def _words(text: str) -> Set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def contains_spoiler(text: str, correct_answers: Optional[List[str]]) -> bool:
    """Есть ли в тексте значимое слово из правильных ответов.

    Служит быстрой детерминированной страховкой от подсказок во вводной и в
    заголовке: точные совпадения слов (регистр и пунктуация не важны).
    """
    if not text:
        return False
    text_words = _words(text)
    if not text_words:
        return False
    for answer in correct_answers or []:
        if _words(str(answer)) & text_words:
            return True
    return False


def review_title(
    cfg, title: str, correct_answers: Optional[List[str]]
) -> Tuple[bool, str]:
    """Проверить заголовок на спойлер (без LLM)."""
    if contains_spoiler(title, correct_answers):
        return False, "заголовок содержит слово из правильного ответа"
    return True, ""


def _correct_answers(questions: List[Dict[str, Any]]) -> List[str]:
    result: List[str] = []
    for q in questions or []:
        options = q.get("options") or []
        index = q.get("correct_index")
        if isinstance(index, int) and 0 <= index < len(options):
            result.append(str(options[index]))
    return result


def _build_prompt(
    intro: str,
    outro: str,
    questions: List[Dict[str, Any]],
    previous_intros: Optional[List[str]],
    check_intro: bool,
    check_outro: bool,
    theme: Optional[str] = None,
) -> str:
    topics = [
        str(q.get("topic", "")).strip()
        for q in questions or []
        if str(q.get("topic", "")).strip()
    ]
    pairs = []
    for q in questions or []:
        question = str(q.get("question", "")).strip()
        options = q.get("options") or []
        index = q.get("correct_index")
        answer = (
            str(options[index])
            if isinstance(index, int) and 0 <= index < len(options)
            else ""
        )
        if question:
            pairs.append(f"- {question} | верный ответ: {answer}")
    questions_block = "\n".join(pairs[:30])

    parts: List[str] = []
    if check_intro:
        parts.append(f"ВВОДНАЯ:\n{intro}")
    if check_outro:
        parts.append(f"ЗАКЛЮЧИТЕЛЬНАЯ:\n{outro}")

    previous_block = ""
    if check_intro and previous_intros:
        listed = "\n".join(f"- {text[:300]}" for text in previous_intros[-5:])
        previous_block = (
            "\nВводные прошлых выпусков (проверь, что новая не повторяет их):\n"
            f"{listed}\n"
        )

    rules: List[str] = []
    if check_intro:
        rules += [
            "- во вводной встречается правильный ответ, его синоним или "
            "прямая подсказка;",
            "- вводная анонсирует область/тему, которой среди фактических "
            "вопросов вообще НЕТ (полное противоречие содержанию). "
            "Перечислить все темы во вводной НЕ требуется: меньший или "
            "обобщённый список — это нормально и НЕ является проблемой;",
            "- вводная шаблонная или повторяет формулировки прошлых выпусков;",
        ]
    if check_outro:
        rules.append(
            "- в заключительной нет инструкции посмотреть правильные ответы или "
            "корректного призыва к действию (подписка/лайк/комментарий)."
        )
    rules_block = "\n".join(rules)

    checked = " и ".join(
        name
        for name, enabled in (("вводную", check_intro), ("заключительную", check_outro))
        if enabled
    )
    theme_line = ""
    if (theme or "").strip():
        theme_line = f"Сквозная тема выпуска: «{theme.strip()}».\n\n"
    return (
        f"Проверь {checked} части теста.\n\n"
        f"{theme_line}"
        f"Темы вопросов: {', '.join(topics) or 'разные области'}.\n\n"
        f"Фактические вопросы и верные ответы:\n{questions_block}\n\n"
        + "\n\n".join(parts)
        + "\n"
        + previous_block
        + "\nСчитай тест НЕ прошедшим ревью, если:\n"
        + rules_block
        + "\n\nВерни JSON строго такого вида:\n"
        '{"ok": true, "issues": []}\n'
        "issues — короткие замечания на русском; если всё хорошо, оставь массив "
        "пустым. Никакого текста кроме JSON."
    )


def _llm_review(
    cfg,
    intro: str,
    outro: str,
    questions: List[Dict[str, Any]],
    previous_intros: Optional[List[str]],
    check_intro: bool,
    check_outro: bool,
    theme: Optional[str] = None,
) -> Dict[str, Any]:
    payload = {
        "model": cfg.effective_review_model(),
        "messages": [
            {"role": "system", "content": REVIEW_SYSTEM},
            {
                "role": "user",
                "content": _build_prompt(
                    intro,
                    outro,
                    questions,
                    previous_intros,
                    check_intro,
                    check_outro,
                    theme,
                ),
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    data = post_chat(cfg, payload)
    content = data["choices"][0]["message"]["content"]
    return extract_json(content)


def review_texts(
    cfg,
    intro: str,
    outro: str,
    questions: List[Dict[str, Any]],
    previous_intros: Optional[List[str]] = None,
    theme: Optional[str] = None,
) -> Dict[str, Any]:
    """Провести ревью вводной/заключительной части.

    Проверяются только включённые части: если `INTRO_ENABLED=false` или
    `CONCLUSION_ENABLED=false`, соответствующая часть из ревью исключается.
    Возвращает ``{"ok": bool, "issues": [str, ...]}``. При выключенном ревью
    всегда OK. При сбое LLM-ревью результат fail-closed (не OK), чтобы не
    публиковать непроверенный выпуск.
    """
    if not getattr(cfg, "review_enabled", True):
        return {"ok": True, "issues": []}

    check_intro = bool(getattr(cfg, "intro_enabled", True)) and bool(
        (intro or "").strip()
    )
    check_outro = bool(getattr(cfg, "conclusion_enabled", True)) and bool(
        (outro or "").strip()
    )
    if not check_intro and not check_outro:
        return {"ok": True, "issues": []}

    issues: List[str] = []
    answers = _correct_answers(questions)
    if check_intro and contains_spoiler(intro, answers):
        issues.append("во вводной встречается слово из правильного ответа")

    try:
        data = _llm_review(
            cfg,
            intro,
            outro,
            questions,
            previous_intros,
            check_intro,
            check_outro,
            theme,
        )
    except Exception as exc:  # noqa: BLE001 - сбой ревью блокирует публикацию
        log.warning("LLM-ревью не выполнено: %s", exc)
        return {
            "ok": False,
            "issues": issues + [f"ревью не выполнено: {exc}"],
        }

    if not isinstance(data, dict):
        log.warning(
            "LLM-ревью вернуло ответ неожиданного типа: %s", type(data).__name__
        )
        return {
            "ok": False,
            "issues": issues + ["ревью вернуло ответ неожиданного формата"],
        }

    if data.get("ok") is not True:
        llm_issues = [
            str(item).strip()
            for item in (data.get("issues") or [])
            if str(item).strip()
        ]
        issues.extend(llm_issues or ["ревью не прошло без пояснений"])

    return {"ok": not issues, "issues": issues}