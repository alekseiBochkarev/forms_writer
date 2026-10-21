"""Тесты репака провала рубрики-ловушки (этап 5).

Покрыто: выбор кандидата по порогам (`wrong_pct`, `answers_count`), отметка
«уже репакован» через `repack_of`, лимиты репака (не более одного репака в
UTC-день и один репак на исходник), отсутствие кандидатов, отбор
неиспользованного шаблона заголовка, публикация репака с пометкой `repack_of`
(без переноса `wrong_pct`), запись состояния до анонса и сухой прогон. Все
сетевые границы (Яндекс Формы, соцсети) заглушены.
"""

from __future__ import annotations

import types

import pytest
from config import Rubric
from config import load_config
from datetime import datetime, timedelta, timezone

import rubric_flow
from state import load_state, record_publish, save_state, today_utc


# --- конфиг и фикстуры -------------------------------------------------------


def _trap_cfg(tmp_path, **overrides):
    """Конфиг репака: ловушка «c» + пороги репака (можно переопределить)."""
    base = {
        "rubrics_state_file": str(tmp_path / "rubrics_state.json"),
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
        "rubric_repack_wrong_pct": 60,
        "rubric_repack_min_answers": 10,
    }
    base.update(overrides)
    cfg = types.SimpleNamespace(**base)
    cfg.rubrics = load_config({"dry_run": True}, require_questions=False).rubrics
    return cfg


def _fail_publish(*a, **k):
    pytest.fail("сеть запрещена — публикация не должна случиться")


# --- выбор кандидата ---------------------------------------------------------


def _state_with(*entries):
    return {"published": [dict(e) for e in entries]}


def _trap_entry(survey_id, *, wrong_pct, answers_count, title="T", **extra):
    entry = {
        "rubric_id": "c",
        "survey_id": survey_id,
        "series_no": 1,
        "count": 10,
        "threshold": 8,
        "wrong_pct": wrong_pct,
        "answers_count": answers_count,
        "title": title,
    }
    entry.update(extra)
    return entry


def test_pick_candidate_below_threshold_none():
    """wrong_pct ниже порога — кандидата нет."""
    state = _state_with(_trap_entry("s1", wrong_pct=59, answers_count=40))
    got = rubric_flow.pick_repack_candidate(
        state, "c", wrong_pct_threshold=60, min_answers=10
    )
    assert got is None


def test_pick_candidate_few_answers_none():
    """Мало ответов (выборка недостаточна) — кандидата нет, даже при high wrong_pct."""
    state = _state_with(_trap_entry("s1", wrong_pct=90, answers_count=9))
    got = rubric_flow.pick_repack_candidate(
        state, "c", wrong_pct_threshold=60, min_answers=10
    )
    assert got is None


def test_pick_candidate_repack_potomok_skipped():
    """Свежий провал — репак-потомок (сам имеет repack_of): он не репакуется,
    а его исходник уже помечен переупакованным — кандидатов нет."""
    state = _state_with(
        _trap_entry("s1", wrong_pct=80, answers_count=30, title="старый"),
        _trap_entry("s2", wrong_pct=90, answers_count=40, repack_of="s1"),
    )
    got = rubric_flow.pick_repack_candidate(
        state, "c", wrong_pct_threshold=60, min_answers=10
    )
    # потомок s2 сам не кандидат, а исходник s1 уже переупакован (ссылка repack_of)
    assert got is None


def test_pick_candidate_skips_repacked_origin_picks_other():
    """Провал-исходник уже переупакован — берётся другой, ещё не репакованный провал."""
    state = _state_with(
        _trap_entry("s1", wrong_pct=80, answers_count=30),
        _trap_entry("r1", wrong_pct=5, answers_count=30, repack_of="s1"),
        _trap_entry("s2", wrong_pct=75, answers_count=25),
    )
    got = rubric_flow.pick_repack_candidate(
        state, "c", wrong_pct_threshold=60, min_answers=10
    )
    assert got is not None and got["survey_id"] == "s2"


def test_pick_candidate_origin_already_repacked_none():
    """Исходный провал уже переупакован (на него ссылается repack_of) — кандидатов нет."""
    state = _state_with(
        _trap_entry("s1", wrong_pct=80, answers_count=30),
        _trap_entry("s2", wrong_pct=10, answers_count=30, repack_of="s1"),
    )
    got = rubric_flow.pick_repack_candidate(
        state, "c", wrong_pct_threshold=60, min_answers=10
    )
    assert got is None


def test_pick_candidate_boundary_inclusive_and_freshest():
    """Границы включаются (wrong_pct==порог, answers==минимум); берётся свежайший."""
    state = _state_with(
        _trap_entry("old", wrong_pct=70, answers_count=20),
        _trap_entry("fresh", wrong_pct=60, answers_count=10),
    )
    got = rubric_flow.pick_repack_candidate(
        state, "c", wrong_pct_threshold=60, min_answers=10
    )
    assert got is not None and got["survey_id"] == "fresh"


def test_pick_candidate_other_rubric_ignored():
    """Записи чужой рубрики не рассматриваются."""
    state = _state_with(
        {
            "rubric_id": "a",
            "survey_id": "x",
            "wrong_pct": 90,
            "answers_count": 40,
        }
    )
    got = rubric_flow.pick_repack_candidate(
        state, "a", wrong_pct_threshold=60, min_answers=10
    )
    assert got is not None and got["survey_id"] == "x"
    got_c = rubric_flow.pick_repack_candidate(
        state, "c", wrong_pct_threshold=60, min_answers=10
    )
    assert got_c is None


def test_pick_candidate_no_metrics_none():
    """Нет метрик (wrong_pct/answers_count отсутствуют) — кандидата нет."""
    state = _state_with({"rubric_id": "c", "survey_id": "s1", "count": 10})
    got = rubric_flow.pick_repack_candidate(
        state, "c", wrong_pct_threshold=60, min_answers=10
    )
    assert got is None


# --- отбор неиспользованного шаблона ----------------------------------------


def _c_rubric(*templates):
    return Rubric(
        id="c",
        series_name="Ловушка",
        source="trap",
        questions_count=10,
        threshold=8,
        comment_prompt="Коммент?",
        title_templates=list(templates),
        enabled=True,
        slot="afternoon",
    )


def test_repack_title_prefers_unused_template():
    """Репак берёт шаблон, ещё не использовавшийся в серии."""
    rubric = _c_rubric("ШАБЛОН-А {series_no}", "ШАБЛОН-Б {series_no}")
    used = ["ШАБЛОН-А №3"]  # А уже светился в серии

    title = rubric_flow._repack_title(rubric, 3, 10, used, wrong_pct="70")

    assert title == "ШАБЛОН-Б №3"


def test_repack_title_falls_back_when_all_used():
    """Если все шаблоны использованы — берётся один из них (повтор допустим)."""
    rubric = _c_rubric("А {series_no}")
    used = ["А №3"]

    title = rubric_flow._repack_title(rubric, 3, 10, used, wrong_pct="70")

    assert title == "А №3"


def test_repack_title_includes_wrong_pct():
    """Динамическая метрика доли ошибшихся попадает в заголовок репака."""
    rubric = _c_rubric("{wrong_pct}% ошиблись {series_no}")

    title = rubric_flow._repack_title(rubric, 5, 10, [], wrong_pct="73")

    assert title == "73% ошиблись №5"


# --- боевой путь репака ------------------------------------------------------


class _RecordingClient:
    def __init__(self, questions):
        self._questions = questions
        self.created = []
        self.published = []
        self.deleted = []
        self.quizzes = []

    def get_questions(self, survey_id):
        # отдают вопросы формы для восстановления репака
        return self._questions

    def next_survey_name(self, base):
        return base

    def create_survey(self, name):
        self.created.append(name)
        return "sid-repack"

    def add_enum_question(self, survey_id, question, options, correct_index,
                          shuffle=True, image=None):
        return 1

    def add_comment_question(self, survey_id, label, header=False):
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
    def __init__(self, client):
        self._client = client

    def __call__(self, *a, **k):
        return self._client

    @staticmethod
    def public_url(survey_id):
        return f"https://forms.yandex.ru/u/{survey_id}/"


def _form_questions(count=10):
    """Вопросы в формате ответа get_questions: label + options[{label,correct}]."""
    return [
        {
            "id": i,
            "label": f"Вопрос {i}?",
            "options": [
                {"label": f"верно {i}", "correct": True},
                {"label": "a", "correct": False},
                {"label": "b", "correct": False},
                {"label": "c", "correct": False},
            ],
        }
        for i in range(1, count + 1)
    ]


def test_repack_publishes_new_form_marks_repack_of(monkeypatch, tmp_path):
    """Репак: новая форма с repack_of исходника, тот же порог и серия, без wrong_pct."""
    client = _RecordingClient(_form_questions(10))
    import yandex_forms

    monkeypatch.setattr(yandex_forms, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        rubric_flow.publisher, "publish_announcement", lambda *a, **k: ["telegram"]
    )

    cfg = _trap_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)
    record_publish(
        state,
        survey_id="origin-1",
        url="https://forms.yandex.ru/u/origin-1/",
        rubric_id="c",
        series_no=1,
        threshold=8,
        count=10,
    )
    last = state["published"][-1]
    last["title"] = "Исходный заголовок"
    last["wrong_pct"] = 82
    last["answers_count"] = 35
    save_state(cfg.rubrics_state_file, state)

    state = load_state(cfg.rubrics_state_file)
    rc = rubric_flow.run_rubric_repack(cfg, state, "c", dry_run=False, force=True)
    assert rc == 0

    saved = load_state(cfg.rubrics_state_file)
    new_entry = saved["published"][-1]
    assert new_entry["rubric_id"] == "c"
    assert new_entry["repack_of"] == "origin-1"
    assert new_entry["survey_id"] == "sid-repack"
    assert new_entry["series_no"] == 1  # серия переупакованного выпуска
    # wrong_pct не переносится: метрику пишет только rubric_metrics по ответам
    assert "wrong_pct" not in new_entry
    assert "answers_count" not in new_entry
    # порог квиза = threshold рубрики, форма опубликована
    assert client.quizzes[0]["quiz"]["pass_scores"] == 8
    assert client.published == ["sid-repack"]


def test_repack_allowed_in_same_utc_day_as_series_publish(monkeypatch, tmp_path):
    """Репак не блокируется публикацией серии в том же UTC-дне (без --force).

    Общее ограничение «раз в день на рубрику» (allow_publish_rubric) к репаку не
    применяется: в CI job репака идёт сразу за выпуском серии «c».
    """
    client = _RecordingClient(_form_questions(10))
    import yandex_forms

    monkeypatch.setattr(yandex_forms, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        rubric_flow.publisher, "publish_announcement", lambda *a, **k: []
    )

    cfg = _trap_cfg(tmp_path, dry_run=False, force=False)
    state = load_state(cfg.rubrics_state_file)
    # свежий выпуск серии «c» сегодня (он же — провальный кандидат на репак)
    record_publish(
        state,
        survey_id="origin-1",
        rubric_id="c",
        series_no=1,
        threshold=8,
        count=10,
    )
    e = state["published"][-1]
    e["title"] = "Исходный"
    e["wrong_pct"] = 82
    e["answers_count"] = 35
    save_state(cfg.rubrics_state_file, state)

    state = load_state(cfg.rubrics_state_file)
    assert rubric_flow.run_rubric_repack(cfg, state, "c", dry_run=False, force=False) == 0
    saved = load_state(cfg.rubrics_state_file)
    assert saved["published"][-1]["repack_of"] == "origin-1"


def test_repack_second_repack_same_day_blocked(monkeypatch, tmp_path):
    """Второй репак в тот же UTC-день заблокирован (сеть не трогается)."""
    import yandex_forms

    monkeypatch.setattr(
        yandex_forms,
        "YandexFormsClient",
        lambda *a, **k: pytest.fail("второй репак за день запрещён — сеть не трогается"),
    )

    cfg = _trap_cfg(tmp_path, dry_run=False, force=False)
    state = load_state(cfg.rubrics_state_file)
    # провал вчера — кандидат есть
    record_publish(
        state,
        survey_id="origin-1",
        rubric_id="c",
        series_no=1,
        threshold=8,
        count=10,
        date=(datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d"),
    )
    e = state["published"][-1]
    e["title"] = "Исходный"
    e["wrong_pct"] = 82
    e["answers_count"] = 35
    # репак сегодня — лимит «один репак в день» исчерпан
    record_publish(
        state,
        survey_id="repack-1",
        rubric_id="c",
        series_no=1,
        threshold=8,
        count=10,
        repack_of="origin-1",
    )
    save_state(cfg.rubrics_state_file, state)

    state = load_state(cfg.rubrics_state_file)
    assert rubric_flow.run_rubric_repack(cfg, state, "c", dry_run=False, force=False) == 0
    assert len(load_state(cfg.rubrics_state_file)["published"]) == 2



def test_repack_source_not_repacked_twice_even_on_other_day(monkeypatch, tmp_path):
    """Один и тот же исходник не репакуется дважды даже в другой день."""
    import yandex_forms

    monkeypatch.setattr(
        yandex_forms,
        "YandexFormsClient",
        lambda *a, **k: pytest.fail("исходник уже переупакован — сеть не трогается"),
    )

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    cfg = _trap_cfg(tmp_path, dry_run=False, force=False)
    state = load_state(cfg.rubrics_state_file)
    record_publish(
        state,
        survey_id="origin-1",
        rubric_id="c",
        series_no=1,
        threshold=8,
        count=10,
        date=(datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d"),
    )
    e = state["published"][-1]
    e["title"] = "Исходный"
    e["wrong_pct"] = 82
    e["answers_count"] = 35
    # репак позавчера — на этот день лимит не действует, но исходник исчерпан
    record_publish(
        state,
        survey_id="repack-1",
        rubric_id="c",
        series_no=1,
        threshold=8,
        count=10,
        date=yesterday,
        publish_ts=f"{yesterday}T13:00:00+00:00",
        repack_of="origin-1",
    )
    save_state(cfg.rubrics_state_file, state)

    state = load_state(cfg.rubrics_state_file)
    assert rubric_flow.run_rubric_repack(cfg, state, "c", dry_run=False, force=False) == 0
    assert len(load_state(cfg.rubrics_state_file)["published"]) == 2


def test_repack_state_written_before_announcement(monkeypatch, tmp_path):
    """Сбой анонса не откатывает публикацию: состояние записано, код 0."""
    client = _RecordingClient(_form_questions(10))
    import yandex_forms

    monkeypatch.setattr(yandex_forms, "YandexFormsClient", _ClientFactory(client))

    def boom(*a, **k):
        raise RuntimeError("telegram недоступен")

    monkeypatch.setattr(rubric_flow.publisher, "publish_announcement", boom)

    cfg = _trap_cfg(tmp_path, dry_run=False, force=False)
    state = load_state(cfg.rubrics_state_file)
    record_publish(
        state,
        survey_id="origin-1",
        rubric_id="c",
        series_no=1,
        threshold=8,
        count=10,
    )
    e = state["published"][-1]
    e["title"] = "Исходный"
    e["wrong_pct"] = 82
    e["answers_count"] = 35
    save_state(cfg.rubrics_state_file, state)

    state = load_state(cfg.rubrics_state_file)
    assert rubric_flow.run_rubric_repack(cfg, state, "c", dry_run=False, force=False) == 0

    saved = load_state(cfg.rubrics_state_file)
    assert saved["published"][-1]["repack_of"] == "origin-1"
    # повторный запуск в тот же день уже не переупакует исходник
    monkeypatch.setattr(
        yandex_forms,
        "YandexFormsClient",
        lambda *a, **k: pytest.fail("репак уже сделан — сеть не трогается"),
    )
    state = load_state(cfg.rubrics_state_file)
    assert rubric_flow.run_rubric_repack(cfg, state, "c", dry_run=False, force=False) == 0


def test_repack_next_series_no_not_shifted_by_repack(tmp_path):
    """Репак-потомок с тем же series_no не сдвигает счётчик выпусков серии."""
    from state import next_series_no

    state = {"published": []}
    record_publish(
        state, survey_id="s1", rubric_id="c", series_no=1, threshold=8, count=10
    )
    assert next_series_no(state, "c") == 2
    record_publish(
        state,
        survey_id="r1",
        rubric_id="c",
        series_no=1,
        threshold=8,
        count=10,
        repack_of="s1",
    )
    # серия репака = серия исходника, следующий выпуск — снова 2
    assert next_series_no(state, "c") == 2
    record_publish(
        state, survey_id="s2", rubric_id="c", series_no=2, threshold=8, count=10
    )
    assert next_series_no(state, "c") == 3


def test_repack_today_utc_helpers_concurrent():
    """Лимит репака привязан к UTC-дню: репак позавчера — не «сегодняшний»."""
    assert today_utc() == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_before = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    assert rubric_flow._already_repacked_today(
        {
            "published": [
                {
                    "rubric_id": "c",
                    "survey_id": "r1",
                    "repack_of": "s1",
                    "publish_ts": f"{day_before}T13:00:00+00:00",
                }
            ]
        },
        "c",
        "s2",
    ) is False


def test_repack_second_run_finds_no_candidate(monkeypatch, tmp_path):
    """После репака повторный прогон не находит кандидатов (репак-потомок не репакуется)."""
    client = _RecordingClient(_form_questions(10))
    import yandex_forms

    monkeypatch.setattr(yandex_forms, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        rubric_flow.publisher, "publish_announcement", lambda *a, **k: []
    )

    cfg = _trap_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)
    record_publish(
        state,
        survey_id="origin-1",
        rubric_id="c",
        series_no=1,
        threshold=8,
        count=10,
    )
    e = state["published"][-1]
    e["title"] = "Исходный"
    e["wrong_pct"] = 82
    e["answers_count"] = 35
    save_state(cfg.rubrics_state_file, state)

    state = load_state(cfg.rubrics_state_file)
    assert rubric_flow.run_rubric_repack(cfg, state, "c", dry_run=False, force=True) == 0
    # исходник переупакован
    assert load_state(cfg.rubrics_state_file)["published"][-1]["repack_of"] == "origin-1"

    # второй прогон: сеть не трогается, потому что кандидатов нет
    monkeypatch.setattr(
        yandex_forms,
        "YandexFormsClient",
        lambda *a, **k: pytest.fail("кандидатов нет — сеть не трогается"),
    )
    state = load_state(cfg.rubrics_state_file)
    assert rubric_flow.run_rubric_repack(cfg, state, "c", dry_run=False, force=True) == 0


def test_repack_no_candidate_returns_zero_no_network(monkeypatch, tmp_path):
    """Нет кандидатов — код 0 без обращения к сети."""
    import yandex_forms

    monkeypatch.setattr(
        yandex_forms,
        "YandexFormsClient",
        lambda *a, **k: pytest.fail("при отсутствии кандидатов сеть не трогается"),
    )

    cfg = _trap_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)
    record_publish(
        state, survey_id="s1", rubric_id="c", series_no=1, threshold=8, count=10
    )
    e = state["published"][-1]
    e["wrong_pct"] = 10  # не провал
    e["answers_count"] = 50
    save_state(cfg.rubrics_state_file, state)

    state = load_state(cfg.rubrics_state_file)
    assert rubric_flow.run_rubric_repack(cfg, state, "c", dry_run=False, force=True) == 0


def test_repack_non_trap_rubric_skipped(tmp_path):
    """Фото-рубрика (не trap) — репак не применяется, код 0, без сети."""
    cfg = _trap_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)
    record_publish(
        state, survey_id="s1", rubric_id="a", series_no=1, threshold=6, count=8
    )
    e = state["published"][-1]
    e["wrong_pct"] = 90
    e["answers_count"] = 40
    save_state(cfg.rubrics_state_file, state)

    state = load_state(cfg.rubrics_state_file)
    assert rubric_flow.run_rubric_repack(cfg, state, "a", dry_run=False, force=True) == 0


def test_repack_dry_run_no_publish(monkeypatch, tmp_path):
    """DRY-RUN репака: не публикует и не пишет состояние."""
    monkeypatch.setattr(
        rubric_flow, "_published_questions",
        lambda *a, **k: pytest.fail("в dry-run форма не читается"),
    )
    monkeypatch.setattr(
        rubric_flow, "_publish_plain_form",
        lambda *a, **k: pytest.fail("в dry-run форма не публикуется"),
    )

    cfg = _trap_cfg(tmp_path, dry_run=True, force=True)
    state = load_state(cfg.rubrics_state_file)
    record_publish(
        state, survey_id="origin-1", rubric_id="c", series_no=1, threshold=8, count=10
    )
    e = state["published"][-1]
    e["title"] = "Исходный"
    e["wrong_pct"] = 82
    e["answers_count"] = 35
    save_state(cfg.rubrics_state_file, state)

    state = load_state(cfg.rubrics_state_file)
    assert rubric_flow.run_rubric_repack(cfg, state, "c", dry_run=True, force=True) == 0
    # состояние не изменилось (нет новой записи)
    assert len(load_state(cfg.rubrics_state_file)["published"]) == 1
