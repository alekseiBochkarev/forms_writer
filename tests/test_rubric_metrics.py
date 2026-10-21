"""Тесты rubric_metrics.py: обновление метрик ответов и сводка по рубрикам.

Сетевые границы (ручка ответов Яндекс Форм) заглушены фейковым клиентом —
тесты изолированы от сети и ключей.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import rubric_flow
import rubric_metrics
from helpers import FakeResponse
from rubric_flow import build_rubric_title, latest_wrong_pct
from state import load_state, record_publish, save_state


NOW = datetime(2026, 1, 20, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _published_entry(rubric_id, survey_id, published_at, **extra):
    entry = {
        "survey_id": survey_id,
        "date": published_at.strftime("%Y-%m-%d"),
        "rubric_id": rubric_id,
        "series_no": 1,
        "threshold": 6,
        "count": 8,
        "publish_ts": _iso(published_at),
    }
    entry.update(extra)
    return entry


class FakeFormsClient:
    """Фейковый клиент Яндекс Форм для ручки ответов и вопросов.

    Возвращает ответы формы через фейковую сессию (`session.get` для ручки
    `/answers`), вопросы — напрямую. Повторяет контракт `_iter_answers`.
    """

    base_url = "https://api.forms.yandex.net/v1"

    class _Session:
        def __init__(self, parent):
            self._parent = parent

        def get(self, url, params=None, timeout=None):
            # Одна страница ответов без next.
            return FakeResponse(200, json_data=self._parent.answers_payload)

    def __init__(self, questions, answers_payload):
        self._questions = questions
        self.answers_payload = answers_payload
        self.session = self._Session(self)

    def get_questions(self, survey_id):
        return self._questions


def _two_question_fixture(correct_flags_per_respondent):
    """Две задачи-вопроса + ответы респондентов.

    `correct_flags_per_respondent` — список пар булев (верно ли на q1, q2).
    Формирует payload ручки `/answers` и вопросы с флагами correct.
    """
    questions = [
        {
            "id": 1,
            "type": "enum",
            "label": "Q1?",
            "items": [
                {"label": "a1", "correct": True},
                {"label": "b1", "correct": False},
            ],
        },
        {
            "id": 2,
            "type": "enum",
            "label": "Q2?",
            "items": [
                {"label": "a2", "correct": False},
                {"label": "b2", "correct": True},
            ],
        },
    ]
    answers = []
    for q1_ok, q2_ok in correct_flags_per_respondent:
        answers.append(
            {
                "data": [
                    {"value": "a1" if q1_ok else "b1"},
                    {"value": "b2" if q2_ok else "a2"},
                ]
            }
        )
    answers_payload = {
        "columns": [{"id": 1}, {"id": 2}],
        "answers": answers,
        "next": None,
    }
    return questions, answers_payload


# --- обновление метрик -------------------------------------------------------


def test_refresh_updates_entries_older_than_hours(tmp_path):
    """Запись старше порога получает answers_count, correct_rate, wrong_pct, metrics_ts."""
    published_at = NOW - timedelta(hours=60)  # старше 48 часов
    state = {
        "published": [_published_entry("c", "sid-old", published_at)],
    }
    questions, answers_payload = _two_question_fixture(
        [(True, False), (False, False), (True, False), (False, False)]
    )
    # 4 респондента, из верных 2 ответа q1 -> correct_rate = 2/8 = 0.25, wrong = 75
    client = FakeFormsClient(questions, answers_payload)

    updated = rubric_metrics.refresh_metrics(client, state, hours=48, now=NOW)
    assert updated == 1

    entry = state["published"][0]
    assert entry["answers_count"] == 4
    assert entry["correct_rate"] == pytest.approx(0.25)
    assert entry["wrong_pct"] == 75
    assert entry["metrics_ts"]


def test_refresh_skips_younger_than_hours():
    """Моложе N часов — метрики не считаем."""
    published_at = NOW - timedelta(hours=10)  # моложе 48
    state = {"published": [_published_entry("c", "sid-new", published_at)]}
    questions, answers_payload = _two_question_fixture([(True, True)])
    client = FakeFormsClient(questions, answers_payload)

    updated = rubric_metrics.refresh_metrics(client, state, hours=48, now=NOW)
    assert updated == 0
    assert "answers_count" not in state["published"][0]
    assert "wrong_pct" not in state["published"][0]


def test_refresh_skips_already_measured():
    """Запись с metrics_checked или непустым views_checkpoints пропускается."""
    published_at = NOW - timedelta(hours=100)
    state = {
        "published": [
            _published_entry("c", "sid-done", published_at, metrics_checked=True),
            _published_entry(
                "c", "sid-cp", published_at, views_checkpoints=[{"h": 48, "n": 10}]
            ),
        ]
    }
    questions, answers_payload = _two_question_fixture([(True, True)])
    client = FakeFormsClient(questions, answers_payload)

    updated = rubric_metrics.refresh_metrics(client, state, hours=48, now=NOW)
    assert updated == 0
    for entry in state["published"]:
        assert "wrong_pct" not in entry


def test_refresh_ignores_non_rubric_entries():
    """Записи без rubric_id (основной поток) не трогаем."""
    published_at = NOW - timedelta(hours=100)
    entry = {
        "survey_id": "sid-main",
        "date": published_at.strftime("%Y-%m-%d"),
        "publish_ts": _iso(published_at),
    }
    state = {"published": [entry]}
    questions, answers_payload = _two_question_fixture([(True, True)])
    client = FakeFormsClient(questions, answers_payload)

    assert rubric_metrics.refresh_metrics(client, state, hours=48, now=NOW) == 0
    assert "wrong_pct" not in entry


def test_refresh_swallows_survey_errors():
    """Сбой по одной форме не роняет прогон — запись остаётся без метрик."""

    class BrokenClient(FakeFormsClient):
        def get_questions(self, survey_id):
            raise RuntimeError("network down")

    published_at = NOW - timedelta(hours=100)
    state = {"published": [_published_entry("c", "sid-bad", published_at)]}
    questions, answers_payload = _two_question_fixture([(True, True)])
    client = BrokenClient(questions, answers_payload)

    assert rubric_metrics.refresh_metrics(client, state, hours=48, now=NOW) == 0
    assert "wrong_pct" not in state["published"][0]


# --- отчёт по рубрикам -------------------------------------------------------


def test_build_report_median_and_verdict():
    """Медиана answers_count за 7 дней и пометка «ниже порога»."""
    now = NOW
    state = {
        "published": [
            _published_entry("a", "s1", now - timedelta(days=1), answers_count=40, wrong_pct=30),
            _published_entry("a", "s2", now - timedelta(days=2), answers_count=60, wrong_pct=50),
            _published_entry("a", "s3", now - timedelta(days=3), answers_count=52, wrong_pct=40),
            # старше 7 дней — не попадает в медиану, но влияет на средний wrong_pct
            _published_entry("a", "s4", now - timedelta(days=10), answers_count=999, wrong_pct=70),
            # другая рубрика — своей медианой нет (старая)
            _published_entry("b", "s5", now - timedelta(days=2), answers_count=200, wrong_pct=10),
        ]
    }

    rows = {row["rubric_id"]: row for row in rubric_metrics.build_report(state, min_median=50, now=now)}

    a = rows["a"]
    assert a["publications"] == 4
    # медиана [40, 60, 52] = 52; 52 >= 50 -> не ниже порога
    assert a["median_answers_7d"] == 52
    assert a["below_threshold"] is False
    assert a["avg_wrong_pct"] == pytest.approx((30 + 50 + 40 + 70) / 4)

    b = rows["b"]
    assert b["publications"] == 1
    assert b["median_answers_7d"] == 200
    assert b["below_threshold"] is False


def test_build_report_below_threshold():
    """Медиана ниже RUBRIC_MIN_MEDIAN -> вердикт «ниже порога»."""
    now = NOW
    state = {
        "published": [
            _published_entry("c", "s1", now - timedelta(days=1), answers_count=20, wrong_pct=40),
            _published_entry("c", "s2", now - timedelta(days=2), answers_count=30, wrong_pct=60),
            _published_entry("c", "s3", now - timedelta(days=3), answers_count=25, wrong_pct=50),
        ]
    }
    rows = rubric_metrics.build_report(state, min_median=50, now=now)
    assert len(rows) == 1
    assert rows[0]["median_answers_7d"] == 25
    assert rows[0]["below_threshold"] is True


def test_build_report_median_excludes_old_entries():
    """Записи старше окна в медиану не попадают."""
    now = NOW
    state = {
        "published": [
            _published_entry("a", "s1", now - timedelta(days=1), answers_count=10),
            _published_entry("a", "s2", now - timedelta(days=40), answers_count=1000),
        ]
    }
    rows = {row["rubric_id"]: row for row in rubric_metrics.build_report(state, min_median=50, now=now)}
    assert rows["a"]["median_answers_7d"] == 10


def test_build_report_excludes_repack_entries():
    """Репак-потомки (repack_of) не влияют на медиану и средний wrong_pct рубрики.

    Репак — отдельное событие (перевыпуск тех же вопросов), а не публикация
    серии; в счётчик публикаций он при этом входит.
    """
    now = NOW
    state = {
        "published": [
            _published_entry("c", "s1", now - timedelta(days=3), answers_count=40, wrong_pct=70),
            _published_entry("c", "s2", now - timedelta(days=2), answers_count=60, wrong_pct=30),
            # репак провала s1: свои метрики не должны удваивать вклад провала
            _published_entry(
                "c",
                "r1",
                now - timedelta(days=1),
                answers_count=1000,
                wrong_pct=90,
                repack_of="s1",
            ),
        ]
    }
    rows = rubric_metrics.build_report(state, min_median=50, now=now)
    assert len(rows) == 1
    row = rows[0]
    assert row["publications"] == 3  # репак — запись публикации
    # медиана [40, 60] = 50 (1000 от репака исключён)
    assert row["median_answers_7d"] == 50
    assert row["avg_wrong_pct"] == pytest.approx((70 + 30) / 2)


def test_render_text_contains_rows_and_threshold():
    """Текстовая таблица содержит рубрику, медиану и порог."""
    rows = [
        {
            "rubric_id": "c",
            "publications": 2,
            "median_answers_7d": 25.0,
            "avg_wrong_pct": 40.0,
            "below_threshold": True,
        }
    ]
    text = rubric_metrics.render_text(rows, min_median=50)
    assert "c" in text
    assert "25" in text
    assert "ниже порога" in text
    assert "50" in text


def test_render_text_empty():
    assert "нет" in rubric_metrics.render_text([], min_median=50).lower()


# --- wrong_pct попадает в state и его подхватывает рубрика c -----------------


def test_wrong_pct_reaches_state_and_latest_wrong_pct(tmp_path):
    """refresh пишет wrong_pct в state; latest_wrong_pct рубрики c читает его."""
    published_at = NOW - timedelta(hours=60)
    state_file = str(tmp_path / "rubrics_state.json")
    state = load_state(state_file)
    record_publish(
        state,
        survey_id="sid-c",
        url="https://forms.yandex.ru/u/sid-c/",
        rubric_id="c",
        series_no=1,
        threshold=8,
        count=10,
        publish_ts=_iso(published_at),
    )
    questions, answers_payload = _two_question_fixture(
        [(True, False), (False, False), (True, False), (False, False)]
    )
    client = FakeFormsClient(questions, answers_payload)

    assert rubric_metrics.refresh_metrics(client, state, hours=48, now=NOW) == 1
    save_state(state_file, state)

    saved = load_state(state_file)
    assert saved["published"][-1]["wrong_pct"] == 75
    # latest_wrong_pct читает это же поле из состояния рубрики c
    assert latest_wrong_pct(saved, "c") == "75"


def test_build_rubric_title_uses_state_wrong_pct(tmp_path):
    """build_rubric_title рубрики c c wrong_pct из state даёт заголовок с числом."""
    published_at = NOW - timedelta(hours=60)
    state_file = str(tmp_path / "rubrics_state.json")
    state = load_state(state_file)
    record_publish(
        state,
        survey_id="sid-c",
        rubric_id="c",
        series_no=1,
        threshold=8,
        count=10,
        publish_ts=_iso(published_at),
    )
    # имитируем запись метрик, как это делает refresh_metrics
    state["published"][-1]["wrong_pct"] = 62

    from config import load_config

    cfg = load_config({"dry_run": True}, require_questions=False)
    rubric = cfg.rubrics.get("c")
    wrong = latest_wrong_pct(state, "c")
    assert wrong == "62"

    # among templates the {wrong_pct} template renders the number; render_title
    # picks randomly among non-spoiler templates, so verify the metric template
    # specifically by restricting the rubric to that single template.
    metric_template = next(
        t for t in rubric.title_templates if "{wrong_pct}" in t
    )
    import dataclasses

    rubric_with_metric = dataclasses.replace(
        rubric, title_templates=[metric_template]
    )
    title = build_rubric_title(
        rubric_with_metric, series_no=1, count=10, wrong_pct=wrong
    )
    assert "62%" in title
    assert rubric_flow.STATIC_WRONG_PCT not in title

    # без метрики — мягкая статичная формулировка вместо числа
    title_static = build_rubric_title(rubric_with_metric, series_no=1, count=10)
    assert rubric_flow.STATIC_WRONG_PCT in title_static


# --- CLI / smoke -------------------------------------------------------------


def test_cli_reports_without_secrets(tmp_path, capsys, monkeypatch):
    """Без токенов main не падает и печатает отчёт по известным данным."""
    now = datetime.now(timezone.utc)
    state_file = str(tmp_path / "rubrics_state.json")
    state = load_state(state_file)
    record_publish(
        state,
        survey_id="sid-c",
        rubric_id="c",
        series_no=1,
        threshold=8,
        count=10,
        publish_ts=_iso(now - timedelta(days=1)),
    )
    state["published"][-1]["answers_count"] = 25
    state["published"][-1]["wrong_pct"] = 40
    save_state(state_file, state)

    monkeypatch.setattr(
        rubric_metrics, "parse_args", lambda: _Args(state=state_file, hours=48, json=False)
    )

    assert rubric_metrics.main() == 0
    out = capsys.readouterr().out
    assert "c" in out
    assert "25" in out


def test_cli_json_output(tmp_path, capsys, monkeypatch):
    """--json печатает валидный JSON-массив строк отчёта."""
    now = datetime.now(timezone.utc)
    state_file = str(tmp_path / "rubrics_state.json")
    state = load_state(state_file)
    record_publish(
        state,
        survey_id="sid-a",
        rubric_id="a",
        series_no=1,
        threshold=6,
        count=8,
        publish_ts=_iso(now - timedelta(days=1)),
    )
    state["published"][-1]["answers_count"] = 100
    state["published"][-1]["wrong_pct"] = 20
    save_state(state_file, state)

    monkeypatch.setattr(
        rubric_metrics, "parse_args", lambda: _Args(state=state_file, hours=48, json=True)
    )
    monkeypatch.setenv("RUBRIC_MIN_MEDIAN", "50")

    assert rubric_metrics.main() == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["rubric_id"] == "a"
    assert data[0]["median_answers_7d"] == 100


def test_min_median_from_config_garbage_falls_back(monkeypatch):
    """RUBRIC_MIN_MEDIAN грузится через config._get_int: мусор/пусто — дефолт, не падение."""
    import config as config_module

    monkeypatch.setenv("RUBRIC_MIN_MEDIAN", "not-a-number")
    cfg = config_module.load_config({"dry_run": True}, require_questions=False)
    assert cfg.rubric_min_median == config_module.DEFAULT_RUBRIC_MIN_MEDIAN

    monkeypatch.setenv("RUBRIC_MIN_MEDIAN", "")
    cfg = config_module.load_config({"dry_run": True}, require_questions=False)
    assert cfg.rubric_min_median == config_module.DEFAULT_RUBRIC_MIN_MEDIAN

    monkeypatch.setenv("RUBRIC_MIN_MEDIAN", "75")
    cfg = config_module.load_config({"dry_run": True}, require_questions=False)
    assert cfg.rubric_min_median == 75


def test_cli_with_garbage_min_median_does_not_crash(tmp_path, capsys, monkeypatch):
    """CLI не падает на мусорном RUBRIC_MIN_MEDIAN — берётся порог по умолчанию."""
    now = datetime.now(timezone.utc)
    state_file = str(tmp_path / "rubrics_state.json")
    state = load_state(state_file)
    record_publish(
        state,
        survey_id="sid-c",
        rubric_id="c",
        series_no=1,
        threshold=8,
        count=10,
        publish_ts=_iso(now - timedelta(days=1)),
    )
    state["published"][-1]["answers_count"] = 25
    state["published"][-1]["wrong_pct"] = 40
    save_state(state_file, state)

    monkeypatch.setattr(
        rubric_metrics, "parse_args", lambda: _Args(state=state_file, hours=48, json=False)
    )
    monkeypatch.setenv("RUBRIC_MIN_MEDIAN", "oops")

    assert rubric_metrics.main() == 0
    out = capsys.readouterr().out
    assert "25" in out


class _Args:
    def __init__(self, state, hours, json):
        self.state = state
        self.hours = hours
        self.json = json
