"""Тесты main.py: чтение вопросов из файла (без сети и без запуска main)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from main import load_questions_from_file

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