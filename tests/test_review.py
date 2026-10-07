"""Тесты review.py: детерминированные спойлеры и LLM-ревью (сеть замокана)."""

from __future__ import annotations

import types

import pytest

import review


def _question(answer: str = "Иван Васильевич") -> dict:
    return {
        "topic": "советское кино",
        "question": "Кадр из какого фильма?",
        "options": [answer, "A", "B", "C"],
        "correct_index": 0,
    }


# --- contains_spoiler -------------------------------------------------------


def test_contains_spoiler_detects_answer_word():
    """Слово из правильного ответа (от 4 букв) — спойлер."""
    assert review.contains_spoiler(
        "Поговорим о фильме «Иван Васильевич меняет профессию».", ["Иван Васильевич"]
    )


def test_contains_spoiler_ignores_short_words():
    """Короткие слова и числа не считаются спойлером."""
    assert not review.contains_spoiler("Тест из 10 вопросов", ["Мышь", "Имя"])


def test_contains_spoiler_no_match():
    """Если пересечения слов нет — спойлера нет."""
    assert not review.contains_spoiler(
        "Вспомним советские фильмы и актёров.", ["Бриллиантовая рука"]
    )


def test_review_title_flags_spoiler():
    """review_title возвращает False и причину при спойлере."""
    ok, reason = review.review_title(None, "Узнайте фильм Иван Васильевич по кадру", ["Иван Васильевич"])
    assert ok is False
    assert reason


def test_review_title_ok_without_spoiler():
    ok, _ = review.review_title(None, "Узнайте советский фильм по кадру", ["Иван Васильевич"])
    assert ok is True


# --- review_texts -----------------------------------------------------------


def _cfg(review_enabled: bool = True, **overrides) -> types.SimpleNamespace:
    base = {
        "review_enabled": review_enabled,
        "intro_enabled": True,
        "conclusion_enabled": True,
        "llm_model": "m",
        "review_model": "",
        "llm_base_url": "https://llm.example/v1",
        "llm_api_key": "k",
        "llm_timeout": 10,
        "llm_retries": 0,
        "llm_retry_delay": 0.0,
        "llm_temperature": 0.0,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_review_texts_disabled_is_ok():
    result = review.review_texts(_cfg(review_enabled=False), "text", "", [_question()])
    assert result == {"ok": True, "issues": []}


def test_review_texts_empty_is_ok():
    result = review.review_texts(_cfg(), "", "", [_question()])
    assert result == {"ok": True, "issues": []}


def test_review_texts_ok_from_llm(monkeypatch):
    """LLM подтвердила ok — issues пуст."""
    monkeypatch.setattr(
        review, "_llm_review", lambda *a, **k: {"ok": True, "issues": []}
    )
    result = review.review_texts(
        _cfg(), "Хорошая вводная про кино", "Спасибо за участие", [_question()]
    )
    assert result["ok"] is True


def test_review_texts_deterministic_spoiler_blocks(monkeypatch):
    """Даже при ok от LLM детерминированный спойлер блокирует публикацию."""
    monkeypatch.setattr(
        review, "_llm_review", lambda *a, **k: {"ok": True, "issues": []}
    )
    result = review.review_texts(
        _cfg(),
        "Сегодня вспомним Иван Васильевич меняет профессию.",
        "Спасибо",
        [_question()],
    )
    assert result["ok"] is False
    assert result["issues"]


def test_review_texts_not_ok_returns_llm_issues(monkeypatch):
    monkeypatch.setattr(
        review,
        "_llm_review",
        lambda *a, **k: {"ok": False, "issues": ["шаблонная вводная"]},
    )
    result = review.review_texts(_cfg(), "вводная", "заключение", [_question()])
    assert result["ok"] is False
    assert "шаблонная вводная" in result["issues"]


def test_review_texts_llm_failure_is_fail_closed(monkeypatch):
    """Сбой LLM-ревью блокирует публикацию (fail-closed)."""

    def boom(*a, **k):
        raise RuntimeError("LLM недоступна")

    monkeypatch.setattr(review, "_llm_review", boom)
    result = review.review_texts(_cfg(), "вводная", "заключение", [_question()])
    assert result["ok"] is False
    assert any("не выполнено" in issue for issue in result["issues"])


@pytest.mark.parametrize("ok_value", ["false", "no", 1, "0", "", None])
def test_review_texts_strict_ok_rejects_non_true(monkeypatch, ok_value):
    """Строковые/числовые значения ok не считаются успехом (fail-closed)."""
    monkeypatch.setattr(
        review,
        "_llm_review",
        lambda *a, **k: {"ok": ok_value, "issues": ["подозрительный ответ"]},
    )
    result = review.review_texts(_cfg(), "вводная", "заключение", [_question()])
    assert result["ok"] is False


def test_review_texts_strict_ok_accepts_only_true(monkeypatch):
    monkeypatch.setattr(
        review, "_llm_review", lambda *a, **k: {"ok": True, "issues": []}
    )
    result = review.review_texts(_cfg(), "вводная", "заключение", [_question()])
    assert result["ok"] is True


def test_review_skips_disabled_outro(monkeypatch):
    """При CONCLUSION_ENABLED=false заключительная не попадает в промпт."""
    captured = {}

    def fake_llm(cfg, intro, outro, questions, previous, check_intro, check_outro):
        captured["check_intro"] = check_intro
        captured["check_outro"] = check_outro
        captured["prompt"] = review._build_prompt(
            intro, outro, questions, previous, check_intro, check_outro
        )
        return {"ok": True, "issues": []}

    monkeypatch.setattr(review, "_llm_review", fake_llm)
    result = review.review_texts(
        _cfg(conclusion_enabled=False), "вводная", "заключение", [_question()]
    )

    assert result["ok"] is True
    assert captured["check_outro"] is False
    assert "ЗАКЛЮЧИТЕЛЬНАЯ" not in captured["prompt"]


def test_review_both_parts_disabled_skips_llm(monkeypatch):
    """Если обе части выключены, ревью не обращается к LLM."""
    monkeypatch.setattr(
        review, "_llm_review", lambda *a, **k: pytest.fail("LLM не должна вызываться")
    )
    result = review.review_texts(
        _cfg(intro_enabled=False, conclusion_enabled=False),
        "вводная",
        "заключение",
        [_question()],
    )
    assert result == {"ok": True, "issues": []}


# --- contains_spoiler: крайние случаи ---------------------------------------


def test_contains_spoiler_is_case_insensitive():
    """Регистр не важен: «СОВЕТСКИЙ» и «советский» считаются спойлером."""
    assert review.contains_spoiler("Это СОВЕТСКИЙ фильм", ["советский"])


def test_contains_spoiler_empty_text_or_answers():
    """Пустой текст/пустой список ответов — спойлера нет."""
    assert review.contains_spoiler("", ["слово"]) is False
    assert review.contains_spoiler("обычный текст", []) is False
    assert review.contains_spoiler("обычный текст", None) is False


def test_contains_spoiler_short_words_do_not_trigger():
    """Короткие слова (<4 букв) не считаются спойлером и не дают ложных срабатываний."""
    assert review.contains_spoiler("Тест из 10 вопросов", ["Мышь", "Имя"]) is False
    assert review.contains_spoiler("Начнём игру", ["идут", "он"]) is False


def test_contains_spoiler_word_forms_not_detected():
    """Документирует ограничение: ловятся только точные словоформы.

    Совпадение по корню/падежу («Иван» против «Ивана») не распознаётся, поэтому
    спойлер в косвенной форме может пройти детерминированную проверку. Это
    ограничение текущей реализации (точное пересечение слов), а не ожидаемое
    поведение по ТЗ — см. баг-репорт.
    """
    assert (
        review.contains_spoiler("Вспомним Ивана Васильевича", ["Иван Васильевич"])
        is False
    )


# --- review_texts: устойчивость к формату ответа LLM ------------------------


def test_review_texts_rejects_int_one_fail_closed(monkeypatch):
    """Целое 1 не приравнивается к True: только `ok is True` проходит ревью."""
    monkeypatch.setattr(
        review,
        "_llm_review",
        lambda *a, **k: {"ok": 1, "issues": ["подозрительный ok"]},
    )
    result = review.review_texts(_cfg(), "вводная", "заключение", [_question()])
    assert result["ok"] is False


def test_review_texts_non_dict_llm_response_is_fail_closed(monkeypatch):
    """Не-dict ответ LLM не роняет поток, а даёт fail-closed (ok=False)."""
    monkeypatch.setattr(review, "_llm_review", lambda *a, **k: ["не объект"])

    result = review.review_texts(_cfg(), "вводная", "заключение", [_question()])

    assert result["ok"] is False
    assert result["issues"]


@pytest.mark.parametrize("payload", [["список"], "строка", 42, None])
def test_review_texts_non_dict_payload_is_fail_closed(monkeypatch, payload):
    """Любой не-dict ответ ревью (в т.ч. str/None) → безопасный отказ ok=False."""
    monkeypatch.setattr(review, "_llm_review", lambda *a, **k: payload)

    result = review.review_texts(_cfg(), "вводная", "заключение", [_question()])

    assert result["ok"] is False
    assert result["issues"]