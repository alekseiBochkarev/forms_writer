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
    rubrics = load_config({"dry_run": True}, require_questions=False).rubrics
    base = {
        "photo_theme": "животные",
        "photo_themes": ["животные"] + list(rubrics.get("b").themes),
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
    """Заглушка photo_flow._build_questions: вернуть готовые вопросы по сущностям."""

    def fake(cfg, thm, question, entities, need):
        limit = min(need, count)
        return [_photo_question(theme, entity) for entity in list(entities)[:limit]]

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

    def spy_title(rubric, series_no, count, used_titles=None, correct_answers=None, **kw):
        captured["series_no"] = series_no
        captured["count"] = count
        captured["threshold"] = rubric.threshold
        return real_title(rubric, series_no, count, used_titles, correct_answers, **kw)

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

    cfg = _rubric_cfg(
        tmp_path,
        photo_theme="",
        photo_themes=list(cfg_b_themes()),
        dry_run=True,
    )
    state = load_state(cfg.rubrics_state_file)

    # Заголовок должен собраться из шаблонов рубрики b (порог 8, серия №1).
    title = rubric_flow.build_rubric_title(cfg.rubrics.get("b"), 1, 10)
    assert "№1" in title
    assert "8" in title and "10" in title

    assert rubric_flow.run_rubric(cfg, state, "b", dry_run=True) == 0


def cfg_b_themes():
    """Темы рубрики «b» из реестра — используются в тестах, чтобы не зависеть от них вручную."""
    rubrics = load_config({"dry_run": True}, require_questions=False).rubrics
    return list(rubrics.get("b").themes)


def test_rubric_b_theme_comes_from_rubric_not_photo_themes(monkeypatch, tmp_path):
    """Рубрика «b» берёт тему из своего списка: животные вне её тем не выбираются."""
    _no_network(monkeypatch)
    captured = {}

    def spy_dry_run(cfg, state, rubric, theme, series_no):
        captured["theme"] = theme
        return 0

    monkeypatch.setattr(rubric_flow, "_dry_run", spy_dry_run)

    cfg = _rubric_cfg(
        tmp_path,
        photo_theme="",
        photo_themes=["животные Африки", "породы собак"],
        dry_run=True,
    )
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "b", dry_run=True) == 0
    assert captured["theme"] == "породы собак"


def test_rubric_b_title_never_promises_wrong_class(monkeypatch, tmp_path):
    """Заголовок рубрики «b» не обещает «картины» на выпуске про собак."""
    _no_network(monkeypatch)
    cfg = _rubric_cfg(tmp_path, dry_run=True)
    rubric = cfg.rubrics.get("b")

    title = rubric_flow.build_rubric_title(rubric, 1, 10, theme="породы собак")
    assert "картин" not in title.lower()
    assert "№1" in title and "8" in title


def test_title_matches_theme_filters_other_class():
    """title_matches_theme: заголовок про картины не подходит теме про животных."""
    assert photo_flow.title_matches_theme(
        "Вы настоящий искусствовед, если угадаете 8/10 картин по фото",
        "картины из школьной программы",
    )
    assert photo_flow.title_matches_theme(
        "Вы настоящий кинолог, если угадаете 8/10 пород по фото", "породы собак"
    )
    assert not photo_flow.title_matches_theme(
        "Вы настоящий искусствовед, если угадаете 8/10 картин по фото",
        "животные Африки",
    )
    # заголовок без маркеров класса подходит любой теме
    assert photo_flow.title_matches_theme("Знаток по фото №1", "животные Африки")


def test_rubric_dry_run_does_not_touch_network(monkeypatch, tmp_path):
    """DRY-RUN не дёргает сеть: любые внешние вызовы приведут к падению теста."""
    _no_network(monkeypatch)
    cfg = _rubric_cfg(tmp_path, dry_run=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "a", dry_run=True) == 0


# --- боевой путь: комментарий, порог, record_publish -------------------------


def test_rubric_publish_adds_comment_and_segments(monkeypatch, tmp_path):
    """Боевой путь: comment-вопрос из comment_prompt в конце, в квизе сегменты."""
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
    # порог рубрики не передаётся в quiz: в режиме сегментов он игнорируется
    quiz = client.quizzes[0]["quiz"]
    assert "pass_scores" not in quiz
    assert quiz["calc_method"] == "range"
    # сегменты результатов строятся для формы с порогом
    assert len(quiz["items"]) == 4
    assert quiz["items"][-1]["upper_limit"] == 8
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


# --- смешанная рубрика «d»: кадры + текстовые факты --------------------------


def _mixed_cfg(tmp_path, **overrides):
    """Конфиг для смешанной рубрики: тема «советские фильмы», источник ruwiki_film."""
    base = {
        "photo_theme": "",
        "photo_themes": ["советские фильмы"],
        "photo_sources": ["ruwiki_film"],
        "film_ru_enabled": True,
    }
    base.update(overrides)
    return _rubric_cfg(tmp_path, **base)


_TEXT_STEMS = [
    "В каком году состоялась премьера «Москвы слезам не верит»?",
    "Кто сыграл Женю Лукашина в «Иронии судьбы»?",
    "Какой режиссёр поставил «Служебный роман»?",
    "Как называется фильм о Шурике и Лиде из Ленинграда?",
    "Кто озвучил кота Матроскина в мультфильмах о Простоквашино?",
    "Сколько серий в фильме «Место встречи изменить нельзя»?",
    "Какая актриса сыграла Маргариту в «Мастере и Маргарите»?",
    "Кто написал музыку к фильму «Ирония судьбы»?",
    "Как звали героиню Нонны Мордюковой в «Бриллиантовой руке»?",
    "В каком городе происходит действие «Белого солнца пустыни»?",
    "Кто снял фильм «Андрей Рублёв»?",
    "Какая фраза стала визитной карточкой Глеба Жеглова?",
    "Какой фильм получил «Оскар» за лучший иностранный фильм в 1969 году?",
    "Кто сыграл Штирлица в «Семнадцати мгновениях весны»?",
    "Как называется деревня, где живут герои «Простоквашино»?",
    "Кто был режиссёром «Белого солнца пустыни»?",
    "Сколько богатырей изображено на известной картине Васнецова?",
]


def _text_questions(count):
    """Текстовые вопросы без картинок: разные формулировки, без ложного дедупа."""
    result = []
    for i in range(count):
        answer = f"ответ {i + 1}"
        result.append(
            {
                "topic": "Кино",
                "question": _TEXT_STEMS[i % len(_TEXT_STEMS)],
                "options": [answer, "вариант 1", "вариант 2", "вариант 3"],
                "correct_index": 0,
            }
        )
    return result


def test_interleave_questions_starts_with_photo_and_spreads_them():
    """5 кадров на 15 вопросов встают в позиции 1, 4, 7, 10, 13."""
    photos = [f"p{i}" for i in range(1, 6)]
    texts = [f"t{i}" for i in range(1, 11)]

    plan = rubric_flow.interleave_questions(photos, texts)

    assert len(plan) == 15
    assert plan[0] == "p1"
    assert [i + 1 for i, item in enumerate(plan) if item.startswith("p")] == [
        1, 4, 7, 10, 13
    ]
    assert [item for item in plan if item.startswith("t")] == texts


def test_interleave_questions_without_one_block_keeps_other():
    """Если один блок пуст, второй возвращается без изменений."""
    assert rubric_flow.interleave_questions([], ["t1", "t2"]) == ["t1", "t2"]
    assert rubric_flow.interleave_questions(["p1"], []) == ["p1"]


def test_mixed_minimum_not_below_threshold_and_plan_share():
    """Минимум смешанного выпуска: max(порог + 1, 80% плана); для 15/10 — 12."""
    cfg = load_config({"dry_run": True}, require_questions=False)
    rubric = cfg.rubrics.get("d")

    assert rubric_flow._mixed_minimum(rubric) == 12


def test_rubric_d_dry_run_plans_15_questions_without_network(monkeypatch, tmp_path):
    """DRY-RUN рубрики «d»: план 15 вопросов (5 кадров + 10 фактов), порог 10."""
    _no_network(monkeypatch)
    monkeypatch.setattr(
        rubric_flow, "generate_topic_questions",
        lambda *a, **k: pytest.fail("в dry-run LLM не вызывается"),
    )
    monkeypatch.setattr(
        photo_flow, "_build_questions",
        lambda *a, **k: pytest.fail("в dry-run картинки не собираются"),
    )

    captured = {}
    real_title = rubric_flow.build_rubric_title

    def spy_title(rubric, series_no, count, used_titles=None, correct_answers=None, **kw):
        captured["series_no"] = series_no
        captured["count"] = count
        captured["threshold"] = rubric.threshold
        return real_title(rubric, series_no, count, used_titles, correct_answers, **kw)

    monkeypatch.setattr(rubric_flow, "build_rubric_title", spy_title)

    cfg = _mixed_cfg(tmp_path, dry_run=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "d", dry_run=True) == 0
    assert captured == {"series_no": 1, "count": 15, "threshold": 10}
    assert not (tmp_path / "rubrics_state.json").exists()


def test_rubric_d_publish_mixes_photo_and_text_questions(monkeypatch, tmp_path):
    """Боевой путь «d»: 15 вопросов, 5 с картинками, 10 текстовых, чередование."""
    import factcheck

    client = _RecordingClient()
    monkeypatch.setattr(photo_flow, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda cfg, theme, count, used: [f"фильм {i + 1}" for i in range(9)],
    )
    _stub_build_questions(monkeypatch, "советские фильмы", 9)
    monkeypatch.setattr(
        rubric_flow, "generate_topic_questions",
        lambda cfg, topic, count, avoid=None, extra=0: _text_questions(count + extra),
    )
    monkeypatch.setattr(factcheck, "filter_questions", lambda cfg, qs: list(qs))
    monkeypatch.setattr(rubric_flow.publisher, "publish_announcement", lambda *a, **k: ["telegram"])

    cfg = _mixed_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "d", dry_run=False, force=True) == 0

    # в форме 15 вопросов: первый — кадр, дальше кадры в позициях 4, 7, 10, 13
    assert len(client.questions) == 15
    question_texts = [q[0] for q in client.questions]
    assert question_texts[0] == photo_flow.question_text("советские фильмы")
    photo_positions = [
        i + 1 for i, text in enumerate(question_texts)
        if text == photo_flow.question_text("советские фильмы")
    ]
    assert photo_positions == [1, 4, 7, 10, 13]
    assert client.comments == [cfg.rubrics.get("d").comment_prompt]
    assert client.published == ["sid-1"]

    saved = load_state(cfg.rubrics_state_file)
    entry = saved["published"][-1]
    assert entry["rubric_id"] == "d"
    assert entry["series_no"] == 1
    assert entry["threshold"] == 10
    assert entry["count"] == 15
    assert entry["theme"] == "советские фильмы"
    assert entry["mixed"] is True
    assert entry["photo_count"] == 5
    assert entry["text_count"] == 10
    # история: фото идут с префиксом темы, текстовые — полным текстом вопроса
    assert len(saved["asked_questions"]) == 15
    assert saved["asked_questions"][0] == "советские фильмы: фильм 1"
    assert saved["used_entities"]["советские фильмы"] == [
        f"фильм {i + 1}" for i in range(5)
    ]


def test_rubric_d_tops_up_missing_photos_with_text(monkeypatch, tmp_path):
    """Если кадров не хватило, недостающие вопросы добираются текстовыми."""
    import factcheck

    client = _RecordingClient()
    monkeypatch.setattr(photo_flow, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda cfg, theme, count, used: [f"фильм {i + 1}" for i in range(9)],
    )
    _stub_build_questions(monkeypatch, "советские фильмы", 3)
    captured = {}

    def fake_topic(cfg, topic, count, avoid=None, extra=0):
        captured["count"] = count
        return _text_questions(count + extra)

    monkeypatch.setattr(rubric_flow, "generate_topic_questions", fake_topic)
    monkeypatch.setattr(factcheck, "filter_questions", lambda cfg, qs: list(qs))
    monkeypatch.setattr(rubric_flow.publisher, "publish_announcement", lambda *a, **k: [])

    cfg = _mixed_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "d", dry_run=False, force=True) == 0

    # 3 кадра + 12 текстовых = 15 вопросов плана
    assert captured["count"] == 12
    saved = load_state(cfg.rubrics_state_file)
    assert saved["published"][-1]["photo_count"] == 3
    assert saved["published"][-1]["text_count"] == 12


def test_rubric_d_tops_up_missing_text_with_photos(monkeypatch, tmp_path):
    """Если текстовых вопросов не хватило, недостающие добираются кадрами."""
    import factcheck

    client = _RecordingClient()
    monkeypatch.setattr(photo_flow, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda cfg, theme, count, used: [f"фильм {i + 1}" for i in range(12)],
    )
    _stub_build_questions(monkeypatch, "советские фильмы", 12)
    monkeypatch.setattr(
        rubric_flow, "generate_topic_questions",
        lambda cfg, topic, count, avoid=None, extra=0: _text_questions(3),
    )
    monkeypatch.setattr(factcheck, "filter_questions", lambda cfg, qs: list(qs))
    monkeypatch.setattr(rubric_flow.publisher, "publish_announcement", lambda *a, **k: [])

    cfg = _mixed_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "d", dry_run=False, force=True) == 0

    entry = load_state(cfg.rubrics_state_file)["published"][-1]
    assert entry["count"] == 15
    assert entry["photo_count"] == 12
    assert entry["text_count"] == 3


def test_rubric_d_aborts_without_text_questions(monkeypatch, tmp_path):
    """Ни одного текстового вопроса — смешанный выпуск отменяется."""
    import factcheck

    monkeypatch.setattr(
        photo_flow, "YandexFormsClient",
        lambda *a, **k: pytest.fail("без текстовых вопросов публикации быть не должно"),
    )
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda cfg, theme, count, used: [f"фильм {i + 1}" for i in range(9)],
    )
    _stub_build_questions(monkeypatch, "советские фильмы", 9)
    monkeypatch.setattr(
        rubric_flow, "generate_topic_questions", lambda *a, **k: []
    )
    monkeypatch.setattr(factcheck, "filter_questions", lambda cfg, qs: list(qs))

    cfg = _mixed_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "d", dry_run=False, force=True) == 1
    assert not (tmp_path / "rubrics_state.json").exists()


def test_rubric_d_aborts_when_mixed_minimum_not_reached(monkeypatch, tmp_path):
    """Собрано меньше минимума (12) — отмена до создания формы."""
    import factcheck

    monkeypatch.setattr(
        photo_flow, "YandexFormsClient",
        lambda *a, **k: pytest.fail("при недоборе форма не создаётся"),
    )
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda cfg, theme, count, used: [f"фильм {i + 1}" for i in range(4)],
    )
    _stub_build_questions(monkeypatch, "советские фильмы", 4)
    monkeypatch.setattr(
        rubric_flow, "generate_topic_questions",
        lambda cfg, topic, count, avoid=None, extra=0: _text_questions(3),
    )
    monkeypatch.setattr(factcheck, "filter_questions", lambda cfg, qs: list(qs))

    cfg = _mixed_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)

    # 4 кадра + 3 текстовых = 7 < 12
    assert rubric_flow.run_rubric(cfg, state, "d", dry_run=False, force=True) == 1
    assert not (tmp_path / "rubrics_state.json").exists()


def test_rubric_d_state_saved_before_announcement(monkeypatch, tmp_path):
    """Сбой анонса не откатывает смешанный выпуск: состояние уже записано."""
    import factcheck

    client = _RecordingClient()
    monkeypatch.setattr(photo_flow, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda cfg, theme, count, used: [f"фильм {i + 1}" for i in range(9)],
    )
    _stub_build_questions(monkeypatch, "советские фильмы", 9)
    monkeypatch.setattr(
        rubric_flow, "generate_topic_questions",
        lambda cfg, topic, count, avoid=None, extra=0: _text_questions(count + extra),
    )
    monkeypatch.setattr(factcheck, "filter_questions", lambda cfg, qs: list(qs))

    def boom(*a, **k):
        raise RuntimeError("telegram недоступен")

    monkeypatch.setattr(rubric_flow.publisher, "publish_announcement", boom)

    cfg = _mixed_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "d", dry_run=False, force=True) == 0
    assert client.published == ["sid-1"]
    saved = load_state(cfg.rubrics_state_file)
    assert saved["published"][-1]["survey_id"] == "sid-1"
    assert saved["published"][-1]["mixed"] is True


def test_mixed_entity_dedup_matches_film_in_text_question():
    """Проверка «тот же фильм»: название из кадра ищется в тексте и вариантах."""
    text = {
        "question": "В каком году вышел фильм «Москва слезам не верит»?",
        "options": ["1979", "1980", "1981", "1982"],
    }
    assert rubric_flow._mentions_any(text, ["Москва слезам не верит"])
    assert not rubric_flow._mentions_any(text, ["Ирония судьбы"])

    photo = {"entity": "Москва слезам не верит"}
    assert rubric_flow._mentions_any_entity(photo, [text])
    assert not rubric_flow._mentions_any_entity({"entity": "Ирония судьбы"}, [text])

    # короткая сущность не должна совпадать с частью другого слова
    assert not rubric_flow._mentions_any(
        {"question": "Который из фильмов вышел позже?", "options": []}, ["кот"]
    )


def test_rubric_d_aborts_without_photos(monkeypatch, tmp_path):
    """Ни одного кадра — смешанный выпуск отменяется без обращения к формам."""
    monkeypatch.setattr(
        photo_flow, "YandexFormsClient",
        lambda *a, **k: pytest.fail("без кадров публикации быть не должно"),
    )
    monkeypatch.setattr(
        photo_flow, "_generate_entities",
        lambda cfg, theme, count, used: [f"фильм {i + 1}" for i in range(5)],
    )
    monkeypatch.setattr(photo_flow, "_build_questions", lambda *a, **k: [])

    cfg = _mixed_cfg(tmp_path, dry_run=False, force=True)
    state = load_state(cfg.rubrics_state_file)

    assert rubric_flow.run_rubric(cfg, state, "d", dry_run=False, force=True) == 1
    assert not (tmp_path / "rubrics_state.json").exists()


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
