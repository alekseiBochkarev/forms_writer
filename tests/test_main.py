"""Тесты main.py: чтение вопросов из файла (без сети и без запуска main)."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

import main as main_module
from dedup import DuplicateChecker
from state import today_utc
from main import (
    _choose_new_tests_theme,
    _collect_unique_questions,
    _generate_texts_with_review,
    _run_new_tests,
    load_questions_from_file,
)

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


# --- _generate_texts_with_review --------------------------------------------


def _texts_cfg(**overrides):
    base = {
        "intro_enabled": True,
        "conclusion_enabled": True,
        "review_enabled": True,
        "review_max_attempts": 1,
        "count": 1,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_generate_texts_and_review_ok_first_try(monkeypatch):
    monkeypatch.setattr("main.generate_intro", lambda cfg, topics, prev=None: "intro")
    monkeypatch.setattr("main.generate_conclusion", lambda cfg, topics, count: "outro")
    monkeypatch.setattr(
        "main.review.review_texts", lambda *a, **k: {"ok": True, "issues": []}
    )

    intro, outro, result = _generate_texts_with_review(
        _texts_cfg(), [_question()], []
    )

    assert intro == "intro"
    assert outro == "outro"
    assert result["ok"] is True


def test_generate_texts_regenerates_on_not_ok(monkeypatch):
    """Не-OK от ревью вызывает одну регенерацию (REVIEW_MAX_ATTEMPTS=1)."""
    calls = {"intro": 0, "review": 0}
    reviews = [
        {"ok": False, "issues": ["спойлер"]},
        {"ok": True, "issues": []},
    ]

    def fake_intro(cfg, topics, prev=None):
        calls["intro"] += 1
        return f"intro-{calls['intro']}"

    monkeypatch.setattr("main.generate_intro", fake_intro)
    monkeypatch.setattr("main.generate_conclusion", lambda *a, **k: "outro")

    def fake_review(*a, **k):
        calls["review"] += 1
        return reviews.pop(0)

    monkeypatch.setattr("main.review.review_texts", fake_review)

    intro, outro, result = _generate_texts_with_review(
        _texts_cfg(), [_question()], []
    )

    assert calls["intro"] == 2
    assert intro == "intro-2"
    assert result["ok"] is True


def test_generate_texts_raises_when_always_not_ok(monkeypatch):
    """Если ревью так и не пройдено — RuntimeError (публикация блокируется)."""
    monkeypatch.setattr("main.generate_intro", lambda *a, **k: "intro")
    monkeypatch.setattr("main.generate_conclusion", lambda *a, **k: "outro")
    monkeypatch.setattr(
        "main.review.review_texts",
        lambda *a, **k: {"ok": False, "issues": ["плохо"]},
    )

    with pytest.raises(RuntimeError):
        _generate_texts_with_review(_texts_cfg(), [_question()], [])


# --- _run_new_tests: dry-run без внешних API --------------------------------


def _new_tests_cfg(state_path, **overrides):
    base = {
        "new_tests_enabled": True,
        "new_tests_state_file": str(state_path),
        "new_tests_count": 10,
        "new_tests_min_questions": 10,
        "new_tests_survey_name": "Насколько широк ваш кругозор",
        "new_tests_tg_target_channel": "@qa_helper_draft",
        "new_tests_publish_telegram": True,
        "new_tests_publish_vk": False,
        "new_tests_topics": ["советское кино", "кулинария"],
        "new_tests_pass_scores": [7, 8],
        "questions_file": None,
        "dry_run": True,
        "force": True,
        "count": 10,
        "topic": "общая эрудиция",
        "survey_name": "Насколько широк ваш кругозор",
        "pass_scores": None,
        "segments": None,
        "yandex_token": "t",
        "yandex_org_id": "org",
        "yandex_org_header": "X-Cloud-Org-Id",
        "llm_api_key": "k",
    }
    base.update(overrides)
    cfg = types.SimpleNamespace(**base)
    cfg.effective_new_tests_topics = lambda: list(cfg.new_tests_topics)
    cfg.effective_new_tests_min_questions = lambda: cfg.new_tests_min_questions
    cfg.effective_new_tests_pass_scores = lambda: list(cfg.new_tests_pass_scores)
    return cfg


def test_run_new_tests_dry_run_builds_plan_without_network(monkeypatch, tmp_path):
    """--new-tests --dry-run: показывает план и не трогает Яндекс/соцсети."""
    monkeypatch.setattr(
        "main._collect_unique_questions",
        lambda cfg, history, checker: [_question() for _ in range(cfg.count)],
    )
    monkeypatch.setattr(
        "main.YandexFormsClient", lambda *a, **k: pytest.fail("сеть запрещена")
    )
    monkeypatch.setattr(
        "main.publish_announcement", lambda *a, **k: pytest.fail("сеть запрещена")
    )

    cfg = _new_tests_cfg(tmp_path / "new_state.json")

    assert _run_new_tests(cfg) == 0
    assert not (tmp_path / "new_state.json").exists()


def test_run_new_tests_disabled_returns_1(monkeypatch):
    monkeypatch.setattr(
        "main.YandexFormsClient", lambda *a, **k: pytest.fail("сеть запрещена")
    )
    cfg = _new_tests_cfg("new_state.json", new_tests_enabled=False, dry_run=False)

    assert _run_new_tests(cfg) == 1


def test_run_new_tests_dry_run_bypasses_disabled_gate(monkeypatch, tmp_path):
    """--dry-run работает даже при NEW_TESTS_ENABLED=false (критерий приёмки 3)."""
    monkeypatch.setattr(
        "main._collect_unique_questions",
        lambda cfg, history, checker: [_question()],
    )
    cfg = _new_tests_cfg(
        tmp_path / "new_state.json",
        new_tests_enabled=False,
        new_tests_count=1,
        new_tests_min_questions=1,
        dry_run=True,
    )

    assert _run_new_tests(cfg) == 0


class _NewTestsClientFactory:
    """Вызываемая замена клиента с сохранением статического public_url."""

    def __init__(self):
        self.created = []

    def __call__(self, *args, **kwargs):
        return self

    def create_survey(self, name):
        self.created.append(name)
        return "sid-1"

    def next_survey_name(self, base):
        return base

    @staticmethod
    def public_url(survey_id):
        return f"https://forms.yandex.ru/u/{survey_id}/"


def test_run_new_tests_publishes_and_writes_state(monkeypatch, tmp_path):
    """Боевой путь: публикация, анонс и запись new_state.json."""
    state_path = tmp_path / "new_state.json"
    calls = {"published": 0, "announced": 0}

    def fake_publish_questions(cfg, questions, client, intro="", outro=""):
        calls["published"] += 1
        return "sid-1"

    def fake_announcement(*a, **k):
        calls["announced"] += 1
        return ["telegram"]

    monkeypatch.setattr("main.YandexFormsClient", _NewTestsClientFactory())
    monkeypatch.setattr("main.publish_questions", fake_publish_questions)
    monkeypatch.setattr("main.publish_announcement", fake_announcement)

    def forbidden(*a, **k):
        raise AssertionError("вопросы уже в файле, LLM не нужна")

    monkeypatch.setattr("main._collect_unique_questions", forbidden)
    monkeypatch.setattr("main.generate_intro", forbidden)
    monkeypatch.setattr("main.generate_conclusion", forbidden)

    cfg = _new_tests_cfg(
        state_path,
        dry_run=False,
        force=True,
        questions_file=str(SAMPLES),
        llm_api_key="",
        intro_enabled=True,
        conclusion_enabled=True,
        review_enabled=True,
        review_max_attempts=1,
    )

    assert _run_new_tests(cfg) == 0
    assert calls == {"published": 1, "announced": 1}

    from state import load_state

    state = load_state(str(state_path))
    assert state["last_publish_date"] == today_utc()
    assert state["published"][0]["survey_id"] == "sid-1"
    assert state["published"][0]["topic"] in cfg.new_tests_topics
    assert state["published"][0]["review"] == {"ok": True, "issues": []}
    assert state["asked_questions"]
    assert state["used_themes"] == [state["published"][0]["topic"]]


def test_run_new_tests_daily_guard_blocks_second_run(monkeypatch, tmp_path):
    """Повтор в тот же день блокируется без внешних вызовов."""
    state_path = tmp_path / "new_state.json"
    state_path.write_text(
        json.dumps({"last_publish_date": today_utc(), "published": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "main.YandexFormsClient", lambda *a, **k: pytest.fail("сеть запрещена")
    )

    cfg = _new_tests_cfg(state_path, dry_run=False, force=False)

    assert _run_new_tests(cfg) == 0


def test_choose_new_tests_theme_rotates_and_resets():
    """Тема выбирается с учётом использованных; при исчерпании список сбрасывается."""
    cfg = _new_tests_cfg("new_state.json")
    state = {"used_themes": ["советское кино"]}

    for _ in range(10):
        assert _choose_new_tests_theme(cfg, state) == "кулинария"

    state["used_themes"] = ["советское кино", "кулинария"]
    chosen = _choose_new_tests_theme(cfg, state)
    assert chosen in {"советское кино", "кулинария"}
    assert state["used_themes"] == []


def test_generate_texts_skips_llm_for_questions_file_without_key(monkeypatch):
    """QUESTIONS_FILE без LLM_API_KEY — тексты и ревью пропускаются без вызовов."""
    monkeypatch.setattr(
        "main.generate_intro", lambda *a, **k: pytest.fail("LLM не должна вызываться")
    )
    monkeypatch.setattr(
        "main.review.review_texts",
        lambda *a, **k: pytest.fail("LLM не должна вызываться"),
    )
    cfg = _new_tests_cfg(
        "new_state.json",
        questions_file="samples/questions.json",
        llm_api_key="",
    )

    intro, outro, result = _generate_texts_with_review(cfg, [_question()], [])

    assert intro == ""
    assert outro == ""
    assert result == {"ok": True, "issues": []}


# --- --survey-id в потоке новых тестов --------------------------------------


def test_run_new_tests_ignores_survey_id_override(monkeypatch, tmp_path):
    """`--survey-id` не действует в режиме --new-tests: форма создаётся новая.

    Новый поток всегда создаёт новую форму, поэтому `cfg.yandex_survey_id`
    сбрасывается в `_run_new_tests`, даже если override пришёл из CLI.
    """
    captured: dict = {}

    def fake_publish_questions(cfg, questions, client, intro="", outro=""):
        captured["survey_id"] = cfg.yandex_survey_id
        captured["questions"] = questions
        return "sid-1"

    monkeypatch.setattr("main.YandexFormsClient", _NewTestsClientFactory())
    monkeypatch.setattr("main.publish_questions", fake_publish_questions)
    monkeypatch.setattr("main.publish_announcement", lambda *a, **k: [])
    monkeypatch.setattr(
        "main._collect_unique_questions",
        lambda *a, **k: pytest.fail("вопросы берутся из файла, LLM не нужна"),
    )

    cfg = _new_tests_cfg(
        tmp_path / "new_state.json",
        dry_run=False,
        force=True,
        questions_file=str(SAMPLES),
        llm_api_key="",
        yandex_survey_id="existing-form-42",
    )

    assert _run_new_tests(cfg) == 0
    # Параметр игнорируется: в публикацию уходит None (создаётся новая форма).
    assert captured["survey_id"] is None


# --- Защита от повторной публикации эрудиции в тот же день -------------------


class _Args:
    """Минимальный namespace для `main.parse_args()`."""

    def __init__(self, **overrides):
        self.dry_run = False
        self.questions_file = None
        self.count = None
        self.topic = None
        self.survey_id = None
        self.name = None
        self.no_publish = False
        self.force = False
        self.check = False
        self.delete_survey = None
        self.rename_survey = None
        self.photo = False
        self.photo_theme = None
        self.new_tests = False
        self.__dict__.update(overrides)


def _erudition_cfg(state_path):
    return types.SimpleNamespace(
        dry_run=False,
        force=False,
        state_file=str(state_path),
        survey_name="Насколько широк ваш кругозор",
        count=13,
        topic="общая эрудиция",
        publish=True,
        questions_file=None,
        yandex_token="t",
        yandex_org_id="org",
        yandex_org_header="X-Cloud-Org-Id",
        llm_api_key="k",
    )


def test_main_erudition_daily_guard_blocks_second_run(monkeypatch, tmp_path):
    """Повторный запуск эрудиции в тот же день блокируется без сети (main())."""
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {"last_publish_date": today_utc(), "published": [], "asked_questions": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(main_module, "parse_args", lambda: _Args())
    monkeypatch.setattr(
        main_module,
        "load_config",
        lambda overrides, require_questions=True: _erudition_cfg(state_path),
    )
    monkeypatch.setattr(
        main_module,
        "YandexFormsClient",
        lambda *a, **k: pytest.fail("сеть запрещена"),
    )
    monkeypatch.setattr(
        "main._collect_unique_questions",
        lambda *a, **k: pytest.fail("вопросы не должны генерироваться"),
    )

    assert main_module.main() == 0
