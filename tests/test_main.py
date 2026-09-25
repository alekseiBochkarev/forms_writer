"""Тесты main.py: чтение вопросов из файла (без сети и без запуска main)."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from dedup import DuplicateChecker
from main import _collect_unique_questions, load_questions_from_file

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples" / "questions.json"


def test_load_samples_file_is_valid():
    """Эталонный samples/questions.json читается и содержит 13 валидных вопросов."""
    questions = load_questions_from_file(str(SAMPLES))

    assert len(questions) == 13
    first = questions[0]
    assert first["question"].startswith("В каком году пала")
    assert len(first["options"]) == 4
    assert first["options"][first["correct_index"]] == "476"


def test_load_file_with_plain_list(tmp_path: Path):
    """Файл-массив (без обёртки questions) тоже поддерживается."""
    path = tmp_path / "list.json"
    path.write_text(
        json.dumps(
            [
                {
                    "topic": "T",
                    "question": "Q?",
                    "options": ["a", "b", "c", "d"],
                    "correct_index": 0,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    questions = load_questions_from_file(str(path))

    assert len(questions) == 1
    assert questions[0]["question"] == "Q?"


def test_load_file_broken_option_count_raises(tmp_path: Path):
    """Вопрос с числом вариантов != 4 — ValueError."""
    path = tmp_path / "broken.json"
    path.write_text(
        json.dumps(
            {
                "questions": [
                    {
                        "question": "Q?",
                        "options": ["a", "b", "c"],
                        "correct_index": 0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_questions_from_file(str(path))


def test_load_file_broken_correct_index_raises(tmp_path: Path):
    """Нецелый correct_index — ValueError."""
    path = tmp_path / "broken.json"
    path.write_text(
        json.dumps(
            {
                "questions": [
                    {
                        "question": "Q?",
                        "options": ["a", "b", "c", "d"],
                        "correct_index": "0",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_questions_from_file(str(path))


def test_load_file_missing_file_raises():
    """Отсутствующий файл — FileNotFoundError (без сети)."""
    with pytest.raises(FileNotFoundError):
        load_questions_from_file("/nonexistent/path/questions.json")


# --- _collect_unique_questions: устойчивость к сбою отдельной попытки --------


def _question():
    return {
        "topic": "История",
        "question": "В каком году?",
        "options": ["1", "2", "3", "4"],
        "correct_index": 2,
    }


def test_collect_unique_survives_transient_failure(monkeypatch):
    """Первый вызов LLM падает, второй успешен — job не роняется."""
    calls = {"n": 0}

    def fake_generate(cfg, avoid=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("LLM timeouts exhausted")
        return [_question()]

    monkeypatch.setattr("main.generate_questions", fake_generate)

    cfg = types.SimpleNamespace(count=1)
    checker = DuplicateChecker()

    result = _collect_unique_questions(cfg, [], checker)

    assert calls["n"] == 2
    assert len(result) == 1
    assert result[0]["question"] == "В каком году?"


def test_collect_unique_raises_only_after_all_attempts(monkeypatch):
    """Все 3 попытки упали — RuntimeError после исчерпания max_attempts."""
    calls = {"n": 0}

    def fake_generate(cfg, avoid=None):
        calls["n"] += 1
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr("main.generate_questions", fake_generate)

    cfg = types.SimpleNamespace(count=1)

    with pytest.raises(RuntimeError):
        _collect_unique_questions(cfg, [], DuplicateChecker())

    assert calls["n"] == 3


def test_collect_unique_keeps_previous_questions_on_failure(monkeypatch):
    """Успешные вопросы предыдущих попыток не теряются при сбое следующей."""
    first = _question()
    second = {**_question(), "question": "Столица Франции?"}
    batches = [[first], RuntimeError("boom"), [second]]

    def fake_generate(cfg, avoid=None):
        item = batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("main.generate_questions", fake_generate)

    cfg = types.SimpleNamespace(count=2)
    checker = DuplicateChecker()

    result = _collect_unique_questions(cfg, [], checker)

    assert [q["question"] for q in result] == [
        "В каком году?",
        "Столица Франции?",
    ]
