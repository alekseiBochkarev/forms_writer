"""Тесты factcheck.py: фильтрация недостоверных вопросов (без сети)."""

from __future__ import annotations

import types

import factcheck
import pytest


def _cfg(enabled=True):
    return types.SimpleNamespace(
        factcheck_enabled=enabled,
        effective_factcheck_model=lambda: "test-model",
    )


def _question(text, answer="верно"):
    return {
        "topic": "T",
        "question": text,
        "options": [answer, "б", "в", "г"],
        "correct_index": 0,
    }


def _llm_returning(monkeypatch, payload):
    monkeypatch.setattr(factcheck, "post_chat", lambda *a, **k: payload)


def _llm_payload(results):
    return {"choices": [{"message": {"content": _json(results)}}]}


def _json(results):
    import json as _j

    return _j.dumps({"results": results}, ensure_ascii=False)


def test_filter_questions_drops_incorrect(monkeypatch):
    """Вопрос с ok=false отбрасывается, остальные остаются."""
    _llm_returning(
        monkeypatch,
        _llm_payload(
            [
                {"index": 0, "ok": True, "reason": ""},
                {"index": 1, "ok": False, "reason": "неверный факт"},
            ]
        ),
    )
    questions = [_question("Вопрос 1"), _question("Вопрос 2")]

    kept = factcheck.filter_questions(_cfg(), questions)

    assert [q["question"] for q in kept] == ["Вопрос 1"]


def test_filter_questions_keeps_missing_indices(monkeypatch):
    """Отсутствующие в ответе вопросы считаются прошедшими."""
    _llm_returning(monkeypatch, _llm_payload([{"index": 0, "ok": False, "reason": "x"}]))
    questions = [_question("A"), _question("B"), _question("C")]

    kept = factcheck.filter_questions(_cfg(), questions)

    assert [q["question"] for q in kept] == ["B", "C"]


def test_filter_questions_disabled_returns_all(monkeypatch):
    """При выключенном факт-чеке LLM не вызывается."""
    monkeypatch.setattr(
        factcheck, "post_chat", lambda *a, **k: pytest.fail("LLM не должна вызываться")
    )
    questions = [_question("A"), _question("B")]

    kept = factcheck.filter_questions(_cfg(enabled=False), questions)

    assert kept == questions


def test_filter_questions_llm_error_keeps_all(monkeypatch):
    """Сбой LLM-проверки не блокирует выпуск — вопросы остаются."""

    def boom(*a, **k):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(factcheck, "post_chat", boom)
    questions = [_question("A"), _question("B")]

    kept = factcheck.filter_questions(_cfg(), questions)

    assert kept == questions


def test_filter_questions_non_dict_keeps_all(monkeypatch):
    """Не-dict ответ факт-чека не роняет поток."""
    monkeypatch.setattr(
        factcheck,
        "post_chat",
        lambda *a, **k: {"choices": [{"message": {"content": "не json-объект"}}]},
    )
    questions = [_question("A")]

    kept = factcheck.filter_questions(_cfg(), questions)

    assert kept == questions


def test_filter_questions_empty_list(monkeypatch):
    monkeypatch.setattr(
        factcheck, "post_chat", lambda *a, **k: pytest.fail("LLM не должна вызываться")
    )
    assert factcheck.filter_questions(_cfg(), []) == []