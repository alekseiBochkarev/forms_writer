"""Тесты dedup.py: канонизация и поиск дублей/похожих формулировок."""

from __future__ import annotations

from dedup import SIMILARITY_THRESHOLD, DuplicateChecker, _similar, canonical


def test_canonical_normalizes_case_punctuation_and_spaces():
    """Канонизация приводит к нижнему регистру и убирает пунктуацию."""
    assert canonical("Кто  это?!") == "кто это"
    assert canonical("  ЁЖИК  ") == "ёжик"
    assert canonical("") == ""


def test_exact_duplicate_detected():
    """Точный повтор (с точностью до регистра/пунктуации) считается дублем."""
    checker = DuplicateChecker(["В каком году пала Римская империя?"])

    assert checker.is_duplicate("в каком году пала римская империя")
    assert checker.is_duplicate("В каком году пала Римская империя?!")


def test_similar_wording_detected():
    """Похожая формулировка выше порога считается дублем."""
    checker = DuplicateChecker(
        ["Кто написал картину «Мона Лиза»?"]
    )

    assert checker.is_duplicate("Кто написал картину Мона Лиза?")
    assert _similar(
        canonical("Кто написал картину «Мона Лиза»?"),
        canonical("Кто написал картину Мона Лиза?"),
    )


def test_different_questions_not_duplicate():
    """Явно разные вопросы не считаются дублями."""
    checker = DuplicateChecker(["Сколько струн у гитары?"])

    assert not checker.is_duplicate("Какая планета самая горячая?")
    assert not checker.is_duplicate("В каком году пала Римская империя?")


def test_empty_string_is_duplicate():
    """Пустая (или пробельная) строка считается дублем и не добавляется."""
    checker = DuplicateChecker()

    assert checker.is_duplicate("")
    assert checker.is_duplicate("   ")
    checker.add("   ")
    assert checker._canon == []


def test_similarity_threshold_is_respected():
    """Порог сравнения: при завышенном пороге похожая строка перестаёт быть дублем."""
    from difflib import SequenceMatcher

    a = canonical("абвгдежзий клмнопрсту")
    b = canonical("абвгдежзий клмнопрсту фхцчшщ")
    ratio = SequenceMatcher(None, a, b).ratio()

    # ratio выше дефолтного порога, но ниже искусственно завышенного
    assert ratio > SIMILARITY_THRESHOLD
    assert _similar(a, b) is True
    assert _similar(a, b, threshold=0.95) is False


def test_add_then_detected():
    """Добавленный вопрос потом обнаруживается как дубль."""
    checker = DuplicateChecker()
    checker.add("Что такое эсперанто?")

    assert checker.is_duplicate("Что такое эсперанто?")