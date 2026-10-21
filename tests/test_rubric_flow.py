"""Тесты rubric_flow.py: dry-run и боевой путь фото-рубрик, отказ trap-рубрики.

Все сетевые границы (LLM, поиск изображений, vision, Яндекс Формы, соцсети)
заглушены — тесты полностью изолированы от сети и ключей.
"""

from __future__ import annotations

import types

import pytest

import photo_flow
import rubric_flow
from config import load_config
from state import load_state, record_publish, save_state, today_utc


# --- фикстуры ----------------------------------------------------------------


def _rubric_cfg(tmp_path, **overrides):
    """Конфиг для рубрик: реестр из config + поля фото-потока для конвейера."""
    base = {
        "photo_theme": "животные",
        "photo_themes": ["животные"],
        "photo_sources": ["wikimedia"],
        "photo_questions_count": 8,
        "photo_min_questions": 1,
        "photo_max_image_attempts": 3,
        "photo_state_file": str(tmp_path / "photo_state.json"),
        "photo_survey_name": "Что на фото",
        "photo_title_templates": None,
        "photo_title_history": 10,
        "photo_flow_enabled": True,
        "film_ru_enabled": False,
        "film_grab_enabled": False,
        "movie_screencaps_enabled": False,
        "wikimedia_user_agent": "test-agent",
        "rubrics_state_file": str(tmp_path / "rubrics_state.json"),
        "shuffle": True,
        "publish": True,
        "number_surveys": False,
        "stats": True,
        "show_results": True,
        "show_correct": True,
        "segments": None,
        "pass_scores": None,
        "dry_run": True,
        "force": False,
        "yandex_token": "t",
        "yandex_org_id": "org",
        "yandex_org_header": "X-Cloud-Org-Id",
        "llm_api_key": "k",
    }
    base.update(overrides)
    cfg = types.SimpleNamespace(**base)
    cfg.rubrics = load_config({"dry_run": True}, require_questions=False).rubrics
    cfg.enabled_photo_sources = lambda: list(cfg.photo_sources or [])
    cfg.effective_photo_themes = lambda: list(cfg.photo_themes or [])
    cfg.effective_photo_min_questions = lambda: cfg.photo_min_questions
    return cfg


class _RecordingClient:
    """Фейковый клиент Яндекс Форм: пишет вызовы, ничего не сетит."""

    def __init__(self):
        self.created = []
        self.questions = []
        self.comments = []
        self.quizzes = []
        self.published = []

    def next_survey_name(self, base):
        return base

    def create_survey(self, name):
        self.created.append(name)
        return "sid-1"

    def upload_image(self, survey_id, data, filename, content_type="image/jpeg"):
        return {"id": f"img-{filename}", "links": {}, "name": filename}

    def add_enum_question(self, survey_id, question, options, correct_index,
                          shuffle=True, image=None):
        self.questions.append((question, options, correct_index))
        return len(self.questions)

    def add_comment_question(self, survey_id, label, header=False):
        self.comments.append(label)
        return 99

    def update_survey(self, survey_id, payload):
        self.quizzes.append(payload)

    def set_access(self, *a, **k):
        pass

    def publish(self, survey_id):
        self.published.append(survey_id)

    def delete_survey(self, *a, **k):
        pass


class _ClientFactory:
    """Вызываемая замена класса клиента с сохранением статического public_url."""

    def __init__(self, client):
        self._client = client

    def __call__(self, *a, **k):
        return self._client

    @staticmethod
    def public_url(survey_id):
        return f"https://forms.yandex.ru/u/{survey_id}/"


def _photo_question(theme, entity):
    return {
        "topic": theme,
        "question": photo_flow.question_text(theme),
        "options": [entity, "x", "y", "z"],
        "correct_index": 0,
        "theme": theme,
        "entity": entity,
        "image": {"source": "wikimedia", "url": "https://x/a.jpg"},
        "_image_bytes": b"bytes",
        "_image_mime": "image/jpeg",
    }


def _stub_build_questions(monkeypatch, theme, count):
    """Заглушка photo_flow._build_questions: вернуть `count` готовых вопросов."""

    def fake(cfg, thm, question, entities, need):
        return [_photo_question(theme, f"сущность {i + 1}") for i in range(min(need, count))]

    monkeypatch.setattr(photo_flow, "_build_questions", fake)


def _no_network(monkeypatch):
    """Запретить любые внешние вызовы в dry-run."""
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda *a, **k: pytest.fail("в dry-run LLM не вызывается"),
    )
    monkeypatch.setattr(
        photo_flow, "YandexFormsClient",
        lambda *a, **k: pytest.fail("в dry-run Яндекс Формы не вызываются"),
    )
    monkeypatch.setattr(
        rubric_flow.publisher, "publish_announcement",
        lambda *a, **k: pytest.fail("в dry-run соцсети не вызываются"),
    )


# --- dry-run рубрик a и b -----------------------------------------------------


def test_rubric_a_dry_run_builds_title_and_questions(monkeypatch, tmp_path):
    """DRY-RUN рубрики «a»: заголовок с №серии и порогом, нужное число вопросов, comment в конце."""
    _no_network(monkeypatch)
    monkeypatch.setattr(photo_flow, "_build_questions",
                        lambda *a, **k: pytest.fail("в dry-run картинки не собираются"))

    captured = {}

    def spy_publish(*args, **kwargs):  # не вызывается в dry-run
        raise AssertionError("публикации в dry-run быть не должно")

    monkeypatch.setattr(rubric_flow, "_publish", spy_publish)

    # Перехватим сборку плана: зафиксируем, что build_rubric_title вызван с серией/порогом.
    real_title = rubric_flow.build_rubric_title

    def spy_title(rubric, series_no, count, used_titles=None, correct_answers=None):
        captured["series_no"] = series_no
        captured["count"] = count
        captured["threshold"] = rubric.threshold
        return real_title(rubric, series_no, count, used_titles, correct_answers)

    monkeypatch.setattr(rubric_flow, "build_rubric_title", spy_title)

    cfg = _rubric_cfg(tmp_path, photo_theme="животные", dry_run=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "a", dry_run=True) == 0
    assert captured == {"series_no": 1, "count": 8, "threshold": 6}
    # dry-run не пишет состояние
    assert not (tmp_path / "rubrics_state.json").exists()


def test_rubric_title_contains_series_no_threshold_and_count():
    """Заголовок рубрики содержит номер серии, порог и число вопросов."""
    cfg = load_config({"dry_run": True}, require_questions=False)
    rubric = cfg.rubrics.get("a")

    title = rubric_flow.build_rubric_title(rubric, series_no=7, count=8)

    assert "№7" in title
    assert str(rubric.threshold) in title  # 6
    assert "8" in title


def test_rubric_b_dry_run_uses_own_title_and_threshold(monkeypatch, tmp_path):
    """DRY-RUN рубрики «b»: свой заголовок (не фото-библиотека) и порог 8/10."""
    _no_network(monkeypatch)

    cfg = _rubric_cfg(tmp_path, photo_theme="породы собак", dry_run=True)
    state = load_state(cfg.rubrics_state_file)

    # Заголовок должен собраться из шаблонов рубрики b (порог 8, серия №1).
    title = rubric_flow.build_rubric_title(cfg.rubrics.get("b"), 1, 10)
    assert "№1" in title
    assert "8" in title and "10" in title

    assert rubric_flow.run_rubric(cfg, state, "b", dry_run=True) == 0


def test_rubric_dry_run_does_not_touch_network(monkeypatch, tmp_path):
    """DRY-RUN не дёргает сеть: любые внешние вызовы приведут к падению теста."""
    _no_network(monkeypatch)
    cfg = _rubric_cfg(tmp_path, dry_run=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "a", dry_run=True) == 0


# --- боевой путь: комментарий, порог, record_publish -------------------------


def test_rubric_publish_adds_comment_and_pass_score(monkeypatch, tmp_path):
    """Боевой путь: comment-вопрос из comment_prompt в конце, порог = threshold."""
    client = _RecordingClient()
    monkeypatch.setattr(photo_flow, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda cfg, theme, count, used: [f"сущность {i + 1}" for i in range(8)],
    )
    _stub_build_questions(monkeypatch, "животные", 8)
    monkeypatch.setattr(rubric_flow.publisher, "publish_announcement", lambda *a, **k: ["telegram"])

    cfg = _rubric_cfg(tmp_path, photo_theme="животные", dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "a", dry_run=False, force=True) == 0

    rubric = cfg.rubrics.get("a")
    # комментарий добавлен ровно один раз и текстом из рубрики
    assert client.comments == [rubric.comment_prompt]
    # порог квиза = threshold рубрики
    quiz = client.quizzes[0]["quiz"]
    assert quiz["pass_scores"] == rubric.threshold
    assert quiz["calc_method"] == "scores"
    assert client.published == ["sid-1"]


def test_rubric_publish_record_publish_fields(monkeypatch, tmp_path):
    """record_publish вызван с rubric_id/series_no/threshold/count и записан в state."""
    client = _RecordingClient()
    monkeypatch.setattr(photo_flow, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda cfg, theme, count, used: [f"сущность {i + 1}" for i in range(8)],
    )
    _stub_build_questions(monkeypatch, "животные", 8)
    monkeypatch.setattr(rubric_flow.publisher, "publish_announcement", lambda *a, **k: [])

    cfg = _rubric_cfg(tmp_path, photo_theme="животные", dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "a", dry_run=False, force=True) == 0

    saved = load_state(cfg.rubrics_state_file)
    entry = saved["published"][-1]
    assert entry["rubric_id"] == "a"
    assert entry["series_no"] == 1
    assert entry["threshold"] == 6
    assert entry["count"] == 8
    assert entry["survey_id"] == "sid-1"
    assert entry["publish_ts"].startswith(today_utc())


def test_rubric_publish_second_run_blocked_same_day(monkeypatch, tmp_path):
    """allow_publish_rubric блокирует второй запуск в тот же UTC-день (без force)."""
    cfg = _rubric_cfg(tmp_path, photo_theme="животные", dry_run=False, force=False)
    state = load_state(cfg.rubrics_state_file)
    # имитируем уже сделанную сегодня публикацию рубрики «a»
    record_publish(state, survey_id="old", rubric_id="a", series_no=1, threshold=6, count=8)
    save_state(cfg.rubrics_state_file, state)

    monkeypatch.setattr(
        photo_flow, "YandexFormsClient",
        lambda *a, **k: pytest.fail("сеть запрещена — публикация заблокирована"),
    )
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda *a, **k: pytest.fail("LLM запрещена — публикация заблокирована"),
    )

    state = load_state(cfg.rubrics_state_file)
    # run_rubric должен вернуться 0 и ничего не делать
    assert rubric_flow.run_rubric(cfg, state, "a", dry_run=False, force=False) == 0

    # force обходит блокировку
    monkeypatch.setattr(photo_flow, "YandexFormsClient", _ClientFactory(_RecordingClient()))
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda cfg, theme, count, used: [f"сущность {i + 1}" for i in range(8)],
    )
    _stub_build_questions(monkeypatch, "животные", 8)
    monkeypatch.setattr(rubric_flow.publisher, "publish_announcement", lambda *a, **k: [])

    state = load_state(cfg.rubrics_state_file)
    assert rubric_flow.run_rubric(cfg, state, "a", dry_run=False, force=True) == 0
    saved = load_state(cfg.rubrics_state_file)
    # force-выпуск получил следующий номер серии
    assert saved["published"][-1]["series_no"] == 2


def test_rubric_publish_state_saved_before_announcement(monkeypatch, tmp_path):
    """Состояние записано до анонса: сбой анонса — warning, выпуск не откатывается.

    Иначе повторный запуск задублировал бы уже опубликованную форму.
    """
    client = _RecordingClient()
    monkeypatch.setattr(photo_flow, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda cfg, theme, count, used: [f"сущность {i + 1}" for i in range(8)],
    )
    _stub_build_questions(monkeypatch, "животные", 8)

    def boom(*a, **k):
        raise RuntimeError("telegram недоступен")

    monkeypatch.setattr(rubric_flow.publisher, "publish_announcement", boom)

    cfg = _rubric_cfg(tmp_path, photo_theme="животные", dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "a", dry_run=False, force=True) == 0
    assert client.published == ["sid-1"]
    saved = load_state(cfg.rubrics_state_file)
    assert saved["published"][-1]["survey_id"] == "sid-1"


def test_rubric_republish_same_day_blocked_after_publish(monkeypatch, tmp_path):
    """Повторный запуск в тот же UTC-день заблокирован allow_publish_rubric."""
    client = _RecordingClient()
    monkeypatch.setattr(photo_flow, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda cfg, theme, count, used: [f"сущность {i + 1}" for i in range(8)],
    )
    _stub_build_questions(monkeypatch, "животные", 8)
    monkeypatch.setattr(rubric_flow.publisher, "publish_announcement", lambda *a, **k: [])

    cfg = _rubric_cfg(tmp_path, photo_theme="животные", dry_run=False, force=False)
    state = load_state(cfg.rubrics_state_file)
    assert rubric_flow.run_rubric(cfg, state, "a", dry_run=False, force=False) == 0
    assert client.published == ["sid-1"]

    # сеть на втором запуске недопустима — блокировка сработала по сохранённому состоянию
    monkeypatch.setattr(
        photo_flow, "YandexFormsClient",
        lambda *a, **k: pytest.fail("повтор в тот же день заблокирован"),
    )
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda *a, **k: pytest.fail("повтор в тот же день заблокирован"),
    )
    state = load_state(cfg.rubrics_state_file)
    assert rubric_flow.run_rubric(cfg, state, "a", dry_run=False, force=False) == 0
    assert len(load_state(cfg.rubrics_state_file)["published"]) == 1


# --- отказ trap-рубрики ------------------------------------------------------

def test_trap_rubric_refuses_with_nonzero_code(monkeypatch, tmp_path):
    """Рубрика source=trap отказывает ненулевым кодом и не лезет в сеть."""
    monkeypatch.setattr(
        photo_flow, "YandexFormsClient",
        lambda *a, **k: pytest.fail("trap-рубрика не должна ничего публиковать"),
    )
    cfg = _rubric_cfg(tmp_path, dry_run=False)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "c", dry_run=False) == 1
    assert not (tmp_path / "rubrics_state.json").exists()


def test_unknown_rubric_returns_error(monkeypatch, tmp_path):
    """Неизвестный id рубрики — ненулевой код без сети."""
    cfg = _rubric_cfg(tmp_path, dry_run=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "nope", dry_run=True) == 1
