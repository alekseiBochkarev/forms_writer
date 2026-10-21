"""Тесты эксперимента с рубриками: реестр в config.py и состояние в state.py."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import config
from config import DEFAULT_RUBRICS, KNOWN_RUBRIC_SOURCES, Rubric, load_rubrics
from state import (
    allow_publish_rubric,
    last_publish_date_rubric,
    load_state,
    next_series_no,
    record_publish,
    today_utc,
)


# --- Реестр рубрик (config.py) -----------------------------------------------


def test_load_rubrics_defaults_when_env_empty():
    """Без RUBRICS берутся три встроенные рубрики a/b/c."""
    rubrics = load_rubrics()

    assert set(rubrics.rubrics) == {"a", "b", "c"}
    a = rubrics.get("a")
    assert a is not None
    assert a.series_name == "Знаток по кино"
    assert a.source == "photo"
    assert a.questions_count == 8
    assert a.threshold == 6
    assert a.title_templates
    assert a.comment_prompt
    assert a.enabled is True

    b = rubrics.get("b")
    assert b is not None
    assert b.source == "photo"
    assert b.questions_count == 10
    assert b.threshold == 8

    c = rubrics.get("c")
    assert c is not None
    assert c.source == "trap"
    assert c.questions_count == 10
    assert c.threshold == 8

    # шаблоны содержат обещанные плейсхолдеры
    assert all("{threshold}" in t and "{count}" in t for t in a.title_templates)
    assert any("{series_no}" in t for t in c.title_templates)


def test_load_rubrics_defaults_are_not_shared_mutable():
    """Дефолтные рубрики не делят списки шаблонов между вызовами."""
    first = load_rubrics()
    first.get("a").title_templates.append("x")
    second = load_rubrics()
    assert "x" not in second.get("a").title_templates


def test_load_rubrics_threshold_not_less_than_count_rejected():
    """threshold >= questions_count — ошибка валидации."""
    payload = [
        {
            "id": "x",
            "series_name": "Тест",
            "source": "photo",
            "questions_count": 8,
            "threshold": 8,
            "title_templates": ["Тест {threshold} из {count}"],
        }
    ]
    os.environ["RUBRICS"] = json.dumps(payload, ensure_ascii=False)
    try:
        with pytest.raises(ValueError) as exc:
            load_rubrics()
    finally:
        del os.environ["RUBRICS"]

    assert "threshold" in str(exc.value)


def test_load_rubrics_unknown_source_rejected():
    """Неизвестный source — ошибка с перечислением допустимых."""
    payload = [
        {
            "id": "x",
            "series_name": "Тест",
            "source": "quizlet",
            "questions_count": 10,
            "threshold": 8,
            "title_templates": ["Тест"],
        }
    ]
    os.environ["RUBRICS"] = json.dumps(payload, ensure_ascii=False)
    try:
        with pytest.raises(ValueError) as exc:
            load_rubrics()
    finally:
        del os.environ["RUBRICS"]

    message = str(exc.value)
    assert "quizlet" in message
    for source in KNOWN_RUBRIC_SOURCES:
        assert source in message


def test_load_rubrics_env_override_and_disabled():
    """RUBRICS из окружения полностью переопределяет встроенный список."""
    payload = [
        {
            "id": "z",
            "series_name": "Своя рубрика",
            "source": "trap",
            "questions_count": 12,
            "threshold": 9,
            "title_templates": ["Сможешь {threshold} из {count}?"],
            "comment_prompt": "Коммент?",
            "enabled": False,
            "slot": "night",
        }
    ]
    os.environ["RUBRICS"] = json.dumps(payload, ensure_ascii=False)
    try:
        rubrics = load_rubrics()
    finally:
        del os.environ["RUBRICS"]

    assert set(rubrics.rubrics) == {"z"}
    assert rubrics.get("z").enabled is False
    assert rubrics.enabled_rubrics() == []


def test_load_config_attaches_rubrics():
    """load_config поднимает реестр рубрик в cfg.rubrics."""
    cfg = config.load_config({"dry_run": True}, require_questions=False)

    assert cfg.rubrics is not None
    assert {r.id for r in cfg.rubrics.enabled_rubrics()} == {"a", "b", "c"}


# --- Состояние рубрик (state.py) ----------------------------------------------


def _entry(rubric_id: str, series_no: int, publish_ts: str) -> dict:
    return {
        "survey_id": f"s{series_no}",
        "date": publish_ts[:10],
        "rubric_id": rubric_id,
        "series_no": series_no,
        "publish_ts": publish_ts,
    }


def test_next_series_no_starts_at_one_and_increments():
    """next_series_no: старт с 1, далее max(series_no)+1 по записям рубрики."""
    state = {"published": [_entry("a", 3, "2026-10-01T06:00:00+00:00")]}
    assert next_series_no(state, "a") == 4
    # другая рубрика не видит чужих номеров
    assert next_series_no(state, "b") == 1
    # записи без series_no (старый формат) игнорируются
    state["published"].append({"survey_id": "old", "date": "2026-09-01"})
    assert next_series_no(state, "old") == 1


def test_next_series_no_ignores_repack_entries():
    """Репак-потомок (repack_of) с тем же series_no не сдвигает номер серии."""
    state = {
        "published": [
            _entry("c", 5, "2026-10-01T06:00:00+00:00"),
            # репак выпуска №5 — та же серия, в счётчик не входит
            {
                "survey_id": "r5",
                "date": "2026-10-01",
                "rubric_id": "c",
                "series_no": 5,
                "publish_ts": "2026-10-01T13:30:00+00:00",
                "repack_of": "s5",
            },
        ]
    }
    assert next_series_no(state, "c") == 6


def test_record_publish_backwards_compatible(tmp_path: Path):
    """record_publish дополняет published; старый state-файл читается без изменений."""
    path = tmp_path / "state.json"
    old_payload = {
        "last_publish_date": "2026-09-01",
        "published": [{"survey_id": "old1", "date": "2026-09-01"}],
        "asked_questions": ["Вопрос"],
    }
    path.write_text(json.dumps(old_payload, ensure_ascii=False), encoding="utf-8")

    state = load_state(str(path))
    entry = record_publish(
        state,
        survey_id="new1",
        url="https://forms.yandex.ru/u/new1/",
        rubric_id="a",
        series_no=1,
        threshold=6,
        count=8,
    )

    assert entry["rubric_id"] == "a"
    assert entry["series_no"] == 1
    assert entry["threshold"] == 6
    assert entry["count"] == 8
    assert entry["url"].startswith("https://")
    assert entry["publish_ts"].startswith(today_utc())
    # старый код: старые поля и структура сохранены
    assert state["published"][0] == old_payload["published"][0]
    assert state["last_publish_date"] == "2026-09-01"

    # repack_of сохраняется только когда задан
    entry2 = record_publish(state, survey_id="new2", rubric_id="a", series_no=2)
    assert "repack_of" not in entry2
    entry3 = record_publish(state, survey_id="new3", repack_of="new2")
    assert entry3["repack_of"] == "new2"


def test_allow_publish_rubric_once_per_utc_day():
    """allow_publish_rubric: дважды за день — False, force — True, другой день — True."""
    today = today_utc()
    state = {
        "published": [_entry("a", 1, f"{today}T05:00:00+00:00")],
    }

    assert allow_publish_rubric(state, "a") is False
    assert allow_publish_rubric(state, "a", force=True) is True
    # рубрика без публикаций за сегодня публикуется свободно
    assert allow_publish_rubric(state, "b") is True

    state["published"].append(_entry("a", 2, "2026-01-01T05:00:00+00:00"))
    assert last_publish_date_rubric(state, "a") == today


def test_allow_publish_rubric_other_day_allows():
    """Публикация вчерашнего дня не блокирует сегодняшний."""
    state = {"published": [_entry("c", 7, "2000-01-01T21:00:00+00:00")]}

    assert last_publish_date_rubric(state, "c") == "2000-01-01"
    assert allow_publish_rubric(state, "c") is True


def test_allow_publish_rubric_empty_state(tmp_path: Path):
    """Пустое состояние (нет published / нет publish_ts) — публикация разрешена."""
    assert allow_publish_rubric({}, "a") is True
    assert last_publish_date_rubric({"published": ["мусор"]}, "a") is None
    # запись рубрики без publish_ts учитывается по полю date
    state = {"published": [{"rubric_id": "a", "date": today_utc()}]}
    assert allow_publish_rubric(state, "a") is False
