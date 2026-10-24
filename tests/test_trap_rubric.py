"""Тесты конвейера вопросов-ловушек (rubric source=trap, этап 3).

Покрыто: парсинг генерации ловушек, фильтрация фактчекинга, отмена при недоборе
минимума, dry-run без сети, публикация (мок Яндекс Форм) с правильными вопросами/
порогом/заголовком/record_publish и дедупликация против истории эрудиционного
слота 1. Все сетевые границы (LLM, фактчек, Яндекс Формы, соцсети) заглушены.
"""

from __future__ import annotations

import json
import types

import pytest
from helpers import FakeResponse

import factcheck
import llm
import rubric_flow
from config import load_config
from state import load_state, record_publish, save_state, today_utc


# --- конфиг и фикстуры -------------------------------------------------------


def _trap_cfg(tmp_path, **overrides):
    """Конфиг для ловушек: реестр рубрик из config + поля, нужные конвейеру."""
    base = {
        "rubrics_state_file": str(tmp_path / "rubrics_state.json"),
        # Основной state.json эрудиции — источник истории слота 1 для дедупа.
        "state_file": str(tmp_path / "state.json"),
        "shuffle": True,
        "publish": True,
        "number_surveys": False,
        "stats": True,
        "show_results": True,
        "show_correct": True,
        "segments": None,
        "pass_scores": None,
        "photo_title_history": 10,
        "dry_run": True,
        "force": False,
        "yandex_token": "t",
        "yandex_org_id": "org",
        "yandex_org_header": "X-Cloud-Org-Id",
        # LLM-параметры для generate_trap_questions.
        "llm_api_key": "k",
        "llm_base_url": "https://llm.example/v1",
        "llm_model": "test-model",
        "llm_temperature": 0.0,
        "llm_timeout": 120,
        "llm_retries": 0,
        "llm_retry_delay": 0.0,
        "language": "ru",
        # Фактчек.
        "factcheck_enabled": True,
        "effective_factcheck_model": lambda: "fact-model",
    }
    base.update(overrides)
    cfg = types.SimpleNamespace(**base)
    cfg.rubrics = load_config({"dry_run": True}, require_questions=False).rubrics
    return cfg


class _RecordingClient:
    """Фейковый клиент Яндекс Форм: пишет вызовы, ничего не сетит."""

    def __init__(self):
        self.created = []
        self.questions = []
        self.comments = []
        self.quizzes = []
        self.published = []
        self.deleted = []

    def next_survey_name(self, base):
        return base

    def create_survey(self, name):
        self.created.append(name)
        return "sid-trap"

    def add_enum_question(self, survey_id, question, options, correct_index,
                          shuffle=True, image=None):
        self.questions.append(
            {"question": question, "options": options,
             "correct_index": correct_index, "image": image}
        )
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

    def delete_survey(self, survey_id):
        self.deleted.append(survey_id)


class _ClientFactory:
    """Вызываемая замена класса клиента со статическим public_url."""

    def __init__(self, client):
        self._client = client

    def __call__(self, *a, **k):
        return self._client

    @staticmethod
    def public_url(survey_id):
        return f"https://forms.yandex.ru/u/{survey_id}/"


_QUESTION_TEMPLATES = [
    "Какая планета Солнечной системы самая большая?",
    "В какой стране изобрели книгопечатание подвижными шрифтами?",
    "Кто написал роман «Война и мир»?",
    "Какой океан самый глубокий на Земле?",
    "Сколько костей у взрослого человека в теле?",
    "Какой химический элемент обозначается символом Au?",
    "В каком году человек впервые побывал на Луне?",
    "Столица Австралии — это какой город?",
    "Какое животное самое быстрое наземное на планете?",
    "Какой язык самый распространённый по числу носителей?",
    "Какая река самая длинная в Европе?",
    "Из чего в основном состоит Солнце?",
]


def _trap(text, answer=None):
    answer = answer or f"верно-{text}"
    return {
        "topic": "Наука",
        "question": text,
        "options": [answer, "ловушка 1", "ловушка 2", "ловушка 3"],
        "correct_index": 0,
    }


def _questions(count, start=1):
    """Разнотемные вопросы-ловушки (не похожи друг на друга — иначе дедуп
    правомерно сочтёт их повторами)."""
    result = []
    for n in range(start, start + count):
        template = _QUESTION_TEMPLATES[(n - 1) % len(_QUESTION_TEMPLATES)]
        result.append(_trap(template, answer=f"верный ответ {n}"))
    return result


def _question_texts(*nums):
    """Тексты вопросов с указанными номерами (для подстановки в историю дедупа)."""
    return [_questions(1, start=n)[0]["question"] for n in nums]


def _fake_llm_questions(monkeypatch, questions):
    """Замокать LLM-вызов generate_trap_questions: вернуть готовые вопросы."""
    content = json.dumps({"questions": questions}, ensure_ascii=False)

    def fake_post(url, **kwargs):
        return FakeResponse(
            200, json_data={"choices": [{"message": {"content": content}}]}
        )

    monkeypatch.setattr("llm.requests.post", fake_post)


def _factcheck_pass_all(monkeypatch):
    monkeypatch.setattr(factcheck, "filter_questions",
                        lambda cfg, qs: list(qs))


def _factcheck_off(monkeypatch):
    monkeypatch.setattr(factcheck, "filter_questions",
                        lambda cfg, qs: list(qs))


# --- генерация ловушек (llm.generate_trap_questions) -------------------------


def test_generate_trap_questions_parses_and_uses_reserve(monkeypatch):
    """Генерация просит count*2 кандидатов и парсит их в общую схему."""
    captured = {}
    want = llm.TRAP_CANDIDATE_FACTOR * 4

    def fake_post(url, **kwargs):
        captured["payload"] = kwargs["json"]
        content = json.dumps({"questions": _questions(want)}, ensure_ascii=False)
        return FakeResponse(
            200, json_data={"choices": [{"message": {"content": content}}]}
        )

    monkeypatch.setattr("llm.requests.post", fake_post)

    result = llm.generate_trap_questions(_trap_cfg(__import__("pathlib").Path(".")), 4)

    assert len(result) == want
    # каждый вопрос прошёл общую валидацию (topic/question/options[4]/correct_index)
    for q in result:
        assert set(q) == {"topic", "question", "options", "correct_index"}
        assert len(q["options"]) == 4
    # запрос был с response_format и моделью из конфига
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["model"] == "test-model"


def test_generate_trap_prompt_mentions_trap_and_one_answer():
    """Промпт требует когнитивную ловушку и ровно один однозначный верный ответ."""
    prompt = llm._build_trap_prompt(8, "ru")
    lowered = prompt.lower()
    assert "ловушк" in lowered
    assert "однозначно верный" in lowered
    assert "4 варианта" in lowered


# --- интеграция: фактчек фильтрует кандидатов --------------------------------


def test_collect_applies_factcheck_and_dedup(monkeypatch):
    """Фактчек и дедуп против истории отсекают лишний, остаются уникальные."""
    # 4 кандидата №1..№4: фактчек отбракует №2, дедуп отбракует №3 (он в истории).
    cands = _questions(4)
    _fake_llm_questions(monkeypatch, cands)
    drop_factcheck = cands[1]["question"]
    dup_in_history = cands[2]["question"]

    def fake_filter(cfg, qs):
        return [q for q in qs if q["question"] != drop_factcheck]

    monkeypatch.setattr(factcheck, "filter_questions", fake_filter)

    cfg = _trap_cfg(__import__("pathlib").Path("."))
    rubric = cfg.rubrics.get("c")
    checker = rubric_flow.DuplicateChecker([dup_in_history])
    collected = rubric_flow._collect_trap_questions(cfg, rubric, checker)

    texts = [q["question"] for q in collected]
    assert texts == [cands[0]["question"], cands[3]["question"]]


# --- отбор: недобор минимума -> отмена выпуска -------------------------------


def test_publish_cancels_when_below_minimum(monkeypatch, tmp_path):
    """Меньше TRAP_MIN_QUESTIONS после фактчека — публикация отменена, сеть не тронута."""
    _fake_llm_questions(monkeypatch, _questions(3))
    _factcheck_off(monkeypatch)
    monkeypatch.setattr(
        rubric_flow, "_publish_plain_form",
        lambda *a, **k: pytest.fail("публикация при недоборе невозможна"),
    )

    cfg = _trap_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "c", dry_run=False, force=True) == 1
    assert not (tmp_path / "rubrics_state.json").exists()


# --- dry-run не дёргает сеть -------------------------------------------------


def test_dry_run_does_not_touch_network(monkeypatch, tmp_path):
    """DRY-RUN ловушки: LLM, фактчек, Яндекс Формы и соцсети не вызываются."""
    monkeypatch.setattr(
        "llm.requests.post",
        lambda *a, **k: pytest.fail("в dry-run LLM не вызывается"),
    )
    monkeypatch.setattr(
        rubric_flow, "_publish_plain_form",
        lambda *a, **k: pytest.fail("в dry-run форма не публикуется"),
    )
    monkeypatch.setattr(
        rubric_flow.publisher, "publish_announcement",
        lambda *a, **k: pytest.fail("в dry-run соцсети не вызываются"),
    )

    cfg = _trap_cfg(tmp_path, dry_run=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "c", dry_run=True) == 0
    # dry-run не пишет состояние
    assert not (tmp_path / "rubrics_state.json").exists()


# --- публикация: вопросы, порог, заголовок, comment, record_publish ----------


def test_publish_sets_questions_threshold_title_comment_and_state(monkeypatch, tmp_path):
    """Боевой путь ловушки: enum без картинок, порог=threshold, заголовок-серия, comment, record_publish."""
    need = 10
    _fake_llm_questions(monkeypatch, _questions(need))
    _factcheck_off(monkeypatch)

    client = _RecordingClient()
    import yandex_forms
    monkeypatch.setattr(yandex_forms, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        rubric_flow.publisher, "publish_announcement", lambda *a, **k: ["telegram"]
    )

    cfg = _trap_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "c", dry_run=False, force=True) == 0

    rubric = cfg.rubrics.get("c")
    # все вопросы добавлены как enum без картинок, с корректными индексами
    assert len(client.questions) == need
    for q in client.questions:
        assert q["image"] is None
        assert 0 <= q["correct_index"] < 4
    # порог рубрики не передаётся в quiz: в режиме сегментов он игнорируется
    quiz = client.quizzes[0]["quiz"]
    assert "pass_scores" not in quiz
    assert quiz["calc_method"] == "range"
    # сегменты результатов строятся для формы с порогом
    assert len(quiz["items"]) == 4
    assert quiz["items"][-1]["upper_limit"] == need
    # comment-вопрос из comment_prompt ровно один
    assert client.comments == [rubric.comment_prompt]
    assert client.published == ["sid-trap"]

    saved = load_state(cfg.rubrics_state_file)
    entry = saved["published"][-1]
    assert entry["rubric_id"] == "c"
    assert entry["series_no"] == 1
    assert entry["threshold"] == rubric.threshold
    assert entry["count"] == need
    assert entry["survey_id"] == "sid-trap"
    # заголовок серии записан и содержит номер серии/порог/число вопросов
    assert entry["title"]
    assert "№1" in entry["title"]
    assert str(rubric.threshold) in entry["title"]
    assert str(need) in entry["title"]


# --- дедупликация против истории эрудиционного слота 1 -----------------------


def test_dedup_against_erudite_history_drops_duplicates(monkeypatch, tmp_path):
    """Вопрос, уже заданный в эрудиционном state.json, отбрасывается из ловушки."""
    # Основной state.json эрудиции — содержит вопросы №2 и №5 из генерируемых.
    dup_texts = _question_texts(2, 5)
    save_state(str(tmp_path / "state.json"), {"asked_questions": dup_texts})
    _fake_llm_questions(monkeypatch, _questions(10))
    _factcheck_off(monkeypatch)

    client = _RecordingClient()
    import yandex_forms
    monkeypatch.setattr(yandex_forms, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        rubric_flow.publisher, "publish_announcement", lambda *a, **k: []
    )

    cfg = _trap_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "c", dry_run=False, force=True) == 0

    published_texts = {q["question"] for q in client.questions}
    # дубликаты из слота 1 не попали в форму
    for text in dup_texts:
        assert text not in published_texts
    # остальных 8 опубликовали (минимум 6 выполнен)
    assert len(client.questions) == 8


# --- динамический {wrong_pct} ------------------------------------------------


def test_wrong_pct_uses_last_metric_from_history():
    """Если в published рубрики есть wrong_pct — берётся последнее округлённое значение."""
    state = {"published": [
        {"rubric_id": "a", "wrong_pct": 99},          # чужая рубрика — игнор
        {"rubric_id": "c", "wrong_pct": 73.6},         # близко к 74
        {"rubric_id": "c", "wrong_pct": 61.2},         # последнее -> 61
    ]}
    assert rubric_flow.latest_wrong_pct(state, "c") == "61"


def _one_template_rubric(template):
    """Рубрика-ловушка с одним шаблоном заголовка (детерминированный рендер)."""
    from config import Rubric

    return Rubric(
        id="c",
        series_name="Ловушка",
        source="trap",
        questions_count=10,
        threshold=8,
        comment_prompt="",
        title_templates=[template],
        enabled=True,
        slot="afternoon",
    )


def test_wrong_pct_placeholder_renders_dynamic_and_soft():
    """{wrong_pct}: динамическая метрика подставляется, иначе — мягкая статичная."""
    rubric = _one_template_rubric("{wrong_pct}% ошиблись: {series_no}")

    dynamic = rubric_flow.build_rubric_title(rubric, 1, 10, wrong_pct="73")
    assert dynamic == "73% ошиблись: №1"

    soft = rubric_flow.build_rubric_title(rubric, 1, 10, wrong_pct=None)
    assert rubric_flow.STATIC_WRONG_PCT in soft
