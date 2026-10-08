"""Тесты photo_flow.py: формулировки, выбор темы, сборка опций и запуск."""

from __future__ import annotations

import llm
import photo_flow
import pytest
import requests
from helpers import FakePhotoCfg, FakeResponse
from photo_sources import ImageCandidate
from state import save_state, today_utc


# --- question_text ----------------------------------------------------------


def test_question_text_film_theme():
    """Темы про фильмы/кино -> «Кадр из какого фильма?»."""
    assert photo_flow.question_text("советские фильмы") == "Кадр из какого фильма?"
    assert photo_flow.question_text("иностранные фильмы") == "Кадр из какого фильма?"
    assert photo_flow.question_text("кино") == "Кадр из какого фильма?"


def test_question_text_actor_theme():
    """Темы про актёров/актрис -> «Кто на фото?»."""
    assert photo_flow.question_text("актёры") == "Кто на фото?"
    assert photo_flow.question_text("актеры") == "Кто на фото?"
    assert photo_flow.question_text("актрисы") == "Кто на фото?"
    assert photo_flow.question_text("советские актёры") == "Кто на фото?"
    assert photo_flow.question_text("советские актрисы") == "Кто на фото?"


def test_question_text_other_theme():
    """Прочие темы -> общая формулировка."""
    assert (
        photo_flow.question_text("животные")
        == "Что (кто) изображено на фотографии?"
    )


def test_image_kind_classifies_theme_for_vision():
    """Класс изображения для vision: film / actor / generic."""
    assert photo_flow.image_kind("советские фильмы") == "film"
    assert photo_flow.image_kind("иностранные фильмы") == "film"
    assert photo_flow.image_kind("советские актёры") == "actor"
    assert photo_flow.image_kind("советские актрисы") == "actor"
    assert photo_flow.image_kind("актёры") == "actor"
    assert photo_flow.image_kind("животные") == "generic"


def test_image_query_for_film_uses_bare_entity():
    """Для кинотемы поисковый запрос — голое название, без префикса film."""
    assert photo_flow.image_query("советские фильмы", "Иван Васильевич") == (
        "Иван Васильевич"
    )
    assert photo_flow.image_query("иностранные фильмы", "The Matrix") == "The Matrix"
    assert photo_flow.image_query("животные", "кот") == "кот"


# --- is_foreign_film_theme --------------------------------------------------


def test_is_foreign_film_theme_categorization():
    """Иностранное кино — фильмы/кино без признаков советского."""
    assert photo_flow.is_foreign_film_theme("иностранные фильмы")
    assert photo_flow.is_foreign_film_theme("кино")
    assert photo_flow.is_foreign_film_theme("зарубежные фильмы")
    assert not photo_flow.is_foreign_film_theme("советские фильмы")
    assert not photo_flow.is_foreign_film_theme("советское кино")
    assert not photo_flow.is_foreign_film_theme("актёры")
    assert not photo_flow.is_foreign_film_theme("животные")


# --- язык названий в промптах LLM -------------------------------------------


def _capture_chat_json(monkeypatch, response):
    """Подменить `_chat_json`, вернув контейнер с захваченным промптом."""
    captured = {}

    def fake_chat_json(cfg, system, user):
        captured["user"] = user
        return response

    monkeypatch.setattr(photo_flow, "_chat_json", fake_chat_json)
    return captured


def test_generate_entities_foreign_films_request_original_titles(monkeypatch):
    """Для иностранных фильмов промпт сущностей требует оригинальные названия."""
    captured = _capture_chat_json(monkeypatch, {"entities": ["The Matrix"]})

    photo_flow._generate_entities(FakePhotoCfg(), "иностранные фильмы", 3, [])

    user = captured["user"]
    assert "ОРИГИНАЛЬНЫЕ" in user
    assert "англ" in user.lower()


def test_generate_entities_foreign_films_keep_dedup(monkeypatch):
    """Требование оригинальных названий не мешает фильтрации повторов."""
    _capture_chat_json(monkeypatch, {"entities": ["The Matrix", "The Matrix", "Alien"]})

    result = photo_flow._generate_entities(FakePhotoCfg(), "кино", 3, [])

    assert result == ["The Matrix", "Alien"]


def test_generate_entities_soviet_films_use_russian(monkeypatch):
    """Для советских фильмов нет требования английских/оригинальных названий."""
    captured = _capture_chat_json(monkeypatch, {"entities": ["Иван Васильевич"]})

    photo_flow._generate_entities(FakePhotoCfg(), "советские фильмы", 3, [])

    user = captured["user"]
    assert "ОРИГИНАЛЬН" not in user
    assert "англ" not in user.lower()
    assert "русском" in user.lower()


def test_generate_entities_other_theme_uses_russian(monkeypatch):
    """Не-кино темы остаются с русскими названиями, без англоязычных требований."""
    captured = _capture_chat_json(monkeypatch, {"entities": ["кот"]})

    photo_flow._generate_entities(FakePhotoCfg(), "животные", 3, [])

    user = captured["user"]
    assert "ОРИГИНАЛЬН" not in user
    assert "англ" not in user.lower()
    assert "русском" in user.lower()


def test_generate_entities_soviet_actresses_hint(monkeypatch):
    """Для темы про советских актрис подсказка просит именно актрис, по-русски."""
    captured = _capture_chat_json(monkeypatch, {"entities": ["Нонна Мордюкова"]})

    photo_flow._generate_entities(FakePhotoCfg(), "советские актрисы", 3, [])

    user = captured["user"].lower()
    assert "актрис" in user
    assert "русском" in user


def test_generate_entities_soviet_actors_hint(monkeypatch):
    """Для темы про советских актёров подсказка просит именно актёров."""
    captured = _capture_chat_json(monkeypatch, {"entities": ["Анатолий Папанов"]})

    photo_flow._generate_entities(FakePhotoCfg(), "советские актёры", 3, [])

    user = captured["user"].lower()
    assert "актёров" in user
    assert "актрис" not in user


def test_generate_distractors_foreign_films_request_original_titles(monkeypatch):
    """Для иностранных фильмов дистракторы — тоже оригинальные названия."""
    captured = _capture_chat_json(monkeypatch, {"distractors": ["Alien", "Jaws", "Heat"]})

    photo_flow._generate_distractors(FakePhotoCfg(), "иностранные фильмы", "The Matrix")

    user = captured["user"]
    assert "ОРИГИНАЛЬНЫЕ" in user
    assert "англ" in user.lower()


def test_generate_distractors_soviet_films_use_russian(monkeypatch):
    """Для советских фильмов дистракторы — русские названия, без английских."""
    captured = _capture_chat_json(monkeypatch, {"distractors": ["Ирония судьбы"]})

    photo_flow._generate_distractors(FakePhotoCfg(), "советские фильмы", "Москва слезам не верит")

    user = captured["user"]
    assert "ОРИГИНАЛЬН" not in user
    assert "англ" not in user.lower()
    assert "русском" in user.lower()


def test_generate_distractors_other_theme_has_no_language_rule(monkeypatch):
    """Для не-кино тем язык дистракторов не навязывается отдельным пунктом."""
    captured = _capture_chat_json(monkeypatch, {"distractors": ["пёс", "лис", "волк"]})

    photo_flow._generate_distractors(FakePhotoCfg(), "животные", "кот")

    user = captured["user"]
    assert "ОРИГИНАЛЬН" not in user
    assert "англ" not in user.lower()
    assert "советских" not in user.lower()


def test_generate_distractors_soviet_actresses_are_actress_names(monkeypatch):
    """Для советских актрис дистракторы — имена актрис, а не названия фильмов."""
    captured = _capture_chat_json(monkeypatch, {"distractors": ["Людмила Гурченко"]})

    photo_flow._generate_distractors(
        FakePhotoCfg(), "советские актрисы", "Нонна Мордюкова"
    )

    user = captured["user"].lower()
    assert "актрис" in user
    assert "советских" in user
    assert "фильм" not in user


def test_generate_distractors_soviet_actors_are_actor_names(monkeypatch):
    """Для советских актёров дистракторы — имена актёров, а не названия фильмов."""
    captured = _capture_chat_json(monkeypatch, {"distractors": ["Юрий Никулин"]})

    photo_flow._generate_distractors(
        FakePhotoCfg(), "советские актёры", "Анатолий Папанов"
    )

    user = captured["user"].lower()
    assert "актёров" in user
    assert "актрис" not in user
    assert "фильм" not in user


# --- живопись: спрашиваем автора, а не название картины ----------------------


def test_question_text_painting_theme_asks_author():
    """Темы про живопись -> «Кто автор этой картины?»."""
    assert photo_flow.question_text("картины") == "Кто автор этой картины?"
    assert (
        photo_flow.question_text("картины из школьной программы")
        == "Кто автор этой картины?"
    )
    assert (
        photo_flow.question_text("пейзажи русских художников")
        == "Кто автор этой картины?"
    )


def test_image_kind_painting_theme():
    """Живопись -> класс изображения painting для vision."""
    assert photo_flow.image_kind("картины") == "painting"
    assert photo_flow.image_kind("пейзажи русских художников") == "painting"


def test_is_painting_theme_detection():
    assert photo_flow.is_painting_theme("картины из школьной программы")
    assert photo_flow.is_painting_theme("Пейзажи русских художников")
    assert photo_flow.is_painting_theme("живопись")
    assert not photo_flow.is_painting_theme("животные")
    assert not photo_flow.is_painting_theme("советские фильмы")


def test_split_painting_and_answer_and_query():
    """Сущность живописи «автор | название» разделяется на ответ и запрос."""
    assert photo_flow.split_painting("Иван Айвазовский|Девятый вал") == (
        "Иван Айвазовский",
        "Девятый вал",
    )
    answer, query = photo_flow.answer_and_query(
        "картины", "Иван Айвазовский|Девятый вал"
    )
    assert answer == "Иван Айвазовский"
    assert query == "Девятый вал"
    # Для обычных тем ответ и запрос совпадают с сущностью.
    assert photo_flow.answer_and_query("животные", "кот") == ("кот", "кот")


def test_split_painting_without_separator_degrades_gracefully():
    """Строка без разделителя не ломает разбор (возвращается как ответ и запрос)."""
    assert photo_flow.split_painting("Девятый вал") == ("Девятый вал", "Девятый вал")


def test_generate_entities_painting_returns_author_and_title(monkeypatch):
    """Для живописи сущности собираются в «автор | название»."""
    _capture_chat_json(
        monkeypatch,
        {
            "entities": [
                {"author": "Иван Айвазовский", "title": "Девятый вал"},
                {"author": "Иван Шишкин", "title": "Утро в сосновом лесу"},
            ]
        },
    )

    result = photo_flow._generate_entities(FakePhotoCfg(), "картины", 2, [])

    assert result == [
        "Иван Айвазовский|Девятый вал",
        "Иван Шишкин|Утро в сосновом лесу",
    ]


def test_generate_entities_painting_prompt_mentions_author(monkeypatch):
    """Промпт живописи просит автора и название, а не только название."""
    captured = _capture_chat_json(
        monkeypatch, {"entities": [{"author": "Иван Шишкин", "title": "Рожь"}]}
    )

    photo_flow._generate_entities(FakePhotoCfg(), "картины", 1, [])

    user = captured["user"].lower()
    assert "автор" in user
    assert "художник" in user


def test_generate_entities_painting_drops_items_without_author(monkeypatch):
    """Записи без автора или названия отбрасываются (нельзя определить ответ)."""
    _capture_chat_json(
        monkeypatch,
        {"entities": [{"author": "", "title": "Рожь"}, {"author": "Иван Шишкин"}]},
    )

    assert photo_flow._generate_entities(FakePhotoCfg(), "картины", 2, []) == []


def test_generate_distractors_painting_are_artists(monkeypatch):
    """Для живописи дистракторы — имена художников."""
    captured = _capture_chat_json(
        monkeypatch, {"distractors": ["Иван Шишкин", "Василий Поленов"]}
    )

    photo_flow._generate_distractors(FakePhotoCfg(), "картины", "Иван Айвазовский")

    user = captured["user"].lower()
    assert "художник" in user
    assert "авторов картин" in user


def test_build_questions_painting_uses_author_answer_and_title_image(monkeypatch):
    """Живопись: верный ответ — автор, изображение ищется по названию картины."""
    cfg = FakePhotoCfg()
    seen = {}

    def fake_build_options(c, theme, entity):
        seen["answer"] = entity
        return (
            ["Иван Айвазовский", "Иван Шишкин", "Василий Поленов", "Карл Брюллов"],
            0,
        )

    def fake_pick_image(c, theme, entity):
        seen["query"] = entity
        cand = ImageCandidate(
            source="wikimedia",
            url="u",
            page_url="p",
            title="t",
            author="a",
            license="CC",
            mime="image/jpeg",
        )
        return b"x", "image/jpeg", cand

    monkeypatch.setattr(photo_flow, "_build_options", fake_build_options)
    monkeypatch.setattr(photo_flow, "_pick_image", fake_pick_image)

    questions = photo_flow._build_questions(
        cfg,
        "картины",
        photo_flow.question_text("картины"),
        ["Иван Айвазовский|Девятый вал"],
        1,
    )

    assert seen["answer"] == "Иван Айвазовский"
    assert seen["query"] == "Девятый вал"
    assert questions[0]["question"] == "Кто автор этой картины?"
    assert questions[0]["options"][questions[0]["correct_index"]] == "Иван Айвазовский"


# --- theme_sources ----------------------------------------------------------


def test_theme_sources_soviet_requires_film_ru_flag():
    """Советские фильмы -> только ruwiki_film, но лишь при включённом флаге."""
    enabled = FakePhotoCfg(film_ru_enabled=True)
    assert photo_flow.theme_sources(enabled, "советские фильмы") == ["ruwiki_film"]
    assert photo_flow.theme_sources(FakePhotoCfg(), "советские фильмы") == []


def test_theme_sources_soviet_actors_and_actresses_use_ruwiki():
    """Советские актёры/актрисы тоже берутся из ruwiki (при FILM_RU_ENABLED)."""
    enabled = FakePhotoCfg(film_ru_enabled=True)
    assert photo_flow.theme_sources(enabled, "советские актёры") == ["ruwiki_film"]
    assert photo_flow.theme_sources(enabled, "советские актрисы") == ["ruwiki_film"]
    assert photo_flow.theme_sources(FakePhotoCfg(), "советские актрисы") == []


def test_theme_sources_foreign_films_use_film_sites():
    """Иностранные фильмы -> filmgrab/movscreencaps по их флагам."""
    cfg = FakePhotoCfg(film_grab_enabled=True, movie_screencaps_enabled=True)
    assert photo_flow.theme_sources(cfg, "иностранные фильмы") == [
        "filmgrab",
        "movscreencaps",
    ]
    assert photo_flow.theme_sources(cfg, "кино") == ["filmgrab", "movscreencaps"]
    assert photo_flow.theme_sources(FakePhotoCfg(), "иностранные фильмы") == []


def test_theme_sources_foreign_films_respects_individual_flags():
    """Каждый кино-источник включается своим флагом."""
    only_grab = FakePhotoCfg(film_grab_enabled=True)
    only_caps = FakePhotoCfg(movie_screencaps_enabled=True)
    assert photo_flow.theme_sources(only_grab, "иностранные фильмы") == ["filmgrab"]
    assert photo_flow.theme_sources(only_caps, "иностранные фильмы") == [
        "movscreencaps"
    ]


def test_theme_sources_other_themes_use_open_sources():
    """Не-кино темы остаются на wikimedia/openverse, без примеси кино-источников."""
    cfg = FakePhotoCfg(film_ru_enabled=True, film_grab_enabled=True)
    assert photo_flow.theme_sources(cfg, "животные") == ["wikimedia", "openverse"]
    assert photo_flow.theme_sources(cfg, "актёры") == ["wikimedia", "openverse"]


# --- available_theme_sources ------------------------------------------------


def test_available_theme_sources_intersects_enabled():
    """Источники темы пересекаются с фактически включёнными в конфиге."""
    cfg = FakePhotoCfg(
        photo_sources=["filmgrab", "movscreencaps"], film_grab_enabled=True
    )
    assert photo_flow.available_theme_sources(cfg, "иностранные фильмы") == [
        "filmgrab"
    ]
    assert photo_flow.available_theme_sources(cfg, "животные") == []


def test_choose_theme_does_not_pick_theme_without_enabled_sources():
    """Тема без реально включённых источников не выбирается.

    PHOTO_SOURCES=["filmgrab","movscreencaps"] выключает wikimedia/openverse,
    поэтому тема «животные» недоступна, хотя статический theme_sources не пуст.
    """
    cfg = FakePhotoCfg(
        photo_themes=["животные", "иностранные фильмы"],
        photo_sources=["filmgrab", "movscreencaps"],
        film_grab_enabled=True,
    )
    assert photo_flow._choose_theme(cfg, {}) == "иностранные фильмы"


# --- _choose_theme ----------------------------------------------------------


def test_choose_theme_uses_explicit_theme():
    """Явно заданная тема возвращается как есть."""
    cfg = FakePhotoCfg(photo_theme="картины")
    state = {"used_themes": ["картины"]}
    assert photo_flow._choose_theme(cfg, state) == "картины"


def test_choose_theme_skips_used_themes():
    """Случайный выбор не возвращает уже использованные темы."""
    cfg = FakePhotoCfg(photo_themes=["животные", "растения", "актёры"])
    state = {"used_themes": ["животные", "растения"]}

    for _ in range(20):
        assert photo_flow._choose_theme(cfg, state) == "актёры"


def test_choose_theme_resets_when_all_used():
    """Когда все темы использованы, список сбрасывается и выбор снова возможен."""
    cfg = FakePhotoCfg(photo_themes=["животные", "растения"])
    state = {"used_themes": ["животные", "растения"]}

    chosen = photo_flow._choose_theme(cfg, state)

    assert chosen in {"животные", "растения"}
    assert state["used_themes"] == []


def test_choose_theme_empty_list_returns_none():
    """Пустой список тем -> None."""
    cfg = FakePhotoCfg(photo_themes=[])
    assert photo_flow._choose_theme(cfg, {}) is None


def test_choose_theme_skips_film_without_flags():
    """Фильмовая тема при выключенных кино-флагах не выбирается."""
    cfg = FakePhotoCfg(photo_themes=["советские фильмы", "иностранные фильмы", "животные"])
    assert photo_flow._choose_theme(cfg, {}) == "животные"


def test_choose_theme_all_films_unavailable_returns_none():
    """Если все темы — фильмовые, а флаги выключены, выбора нет."""
    cfg = FakePhotoCfg(photo_themes=["советские фильмы", "иностранные фильмы"])
    assert photo_flow._choose_theme(cfg, {}) is None


def test_choose_theme_explicit_unavailable_returns_none():
    """Явно заданная фильмовая тема без источников не принимается."""
    cfg = FakePhotoCfg(photo_theme="иностранные фильмы")
    assert photo_flow._choose_theme(cfg, {}) is None


# --- _build_options ---------------------------------------------------------


def test_build_options_shuffles_and_keeps_correct_index(monkeypatch):
    """После перемешивания correct_index указывает на верный ответ, 4 уникальных."""
    monkeypatch.setattr(
        photo_flow,
        "_generate_distractors",
        lambda cfg, theme, entity: ["Б", "В", "Г"],
    )
    cfg = FakePhotoCfg()

    options, correct_index = photo_flow._build_options(cfg, "животные", "А")

    assert len(options) == 4
    assert len(set(options)) == 4
    assert set(options) == {"А", "Б", "В", "Г"}
    assert options[correct_index] == "А"


def test_build_options_drops_duplicate_distractors(monkeypatch):
    """Дистракторы, совпадающие с ответом/между собой, отбрасываются."""
    monkeypatch.setattr(
        photo_flow,
        "_generate_distractors",
        lambda cfg, theme, entity: ["А", "А", "Б", "В", "Г"],
    )
    cfg = FakePhotoCfg()

    options, correct_index = photo_flow._build_options(cfg, "животные", "А")

    assert options[correct_index] == "А"
    assert set(options) == {"А", "Б", "В", "Г"}


def test_build_options_too_few_distractors_returns_none(monkeypatch):
    """Меньше 3 уникальных дистракторов -> None (вопрос не строится)."""
    monkeypatch.setattr(
        photo_flow,
        "_generate_distractors",
        lambda cfg, theme, entity: ["Б", "В"],
    )
    cfg = FakePhotoCfg()

    assert photo_flow._build_options(cfg, "животные", "А") is None


def test_build_options_llm_failure_returns_none(monkeypatch):
    """Сбой генерации дистракторов не роняет поток, а возвращает None."""
    def boom(cfg, theme, entity):
        raise RuntimeError("LLM недоступна")

    monkeypatch.setattr(photo_flow, "_generate_distractors", boom)
    cfg = FakePhotoCfg()

    assert photo_flow._build_options(cfg, "животные", "А") is None


# --- run: выключенный фото-поток -------------------------------------------


def test_run_disabled_returns_1_without_network(monkeypatch):
    """PHOTO_FLOW_ENABLED=false -> код 1 и никаких сетевых вызовов."""
    def forbidden(*args, **kwargs):
        raise AssertionError("поток выключен, сеть недопустима")

    monkeypatch.setattr(llm.requests, "post", forbidden)
    monkeypatch.setattr(photo_flow, "YandexFormsClient", forbidden)

    cfg = FakePhotoCfg(photo_flow_enabled=False)
    assert photo_flow.run(cfg) == 1


# --- _chat_json: ретраи через общий хелпер ----------------------------------


def test_chat_json_retries_on_timeout(monkeypatch):
    """ReadTimeout на первой попытке не роняет фото-поток — запрос повторяется."""
    calls = {"n": 0}

    def fake_post(url, json, headers, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ReadTimeout("timeout")
        return FakeResponse(
            200,
            json_data={
                "choices": [{"message": {"content": '{"entities": ["кот"]}'}}]
            },
        )

    monkeypatch.setattr(llm.requests, "post", fake_post)

    data = photo_flow._chat_json(FakePhotoCfg(), "system", "user")

    assert data == {"entities": ["кот"]}
    assert calls["n"] == 2


# --- run: отмена при недоборе вопросов -------------------------------------


def test_run_cancels_when_below_minimum(monkeypatch, tmp_path):
    """Если подготовлено меньше photo_min_questions — публикации нет, код 1."""
    def forbidden_client(*args, **kwargs):
        raise AssertionError("публикация не должна начинаться")

    monkeypatch.setattr(photo_flow, "YandexFormsClient", forbidden_client)
    monkeypatch.setattr(
        photo_flow, "_generate_entities", lambda cfg, theme, count, used: ["a", "b", "c"]
    )
    monkeypatch.setattr(
        photo_flow,
        "_build_questions",
        lambda cfg, theme, question, entities, need: [
            {"topic": theme, "question": question, "options": [], "correct_index": 0},
            {"topic": theme, "question": question, "options": [], "correct_index": 0},
        ],
    )

    state_path = tmp_path / "photo_state.json"
    cfg = FakePhotoCfg(
        photo_flow_enabled=True,
        dry_run=False,
        force=True,
        photo_theme="животные",
        photo_questions_count=5,
        photo_min_questions=3,
        photo_state_file=str(state_path),
        publish=True,
    )

    result = photo_flow.run(cfg)

    assert result == 1
    assert not state_path.exists()


def test_run_returns_1_when_no_entities(monkeypatch, tmp_path):
    """Модель не вернула сущностей -> код 1 без публикации."""
    monkeypatch.setattr(photo_flow, "_generate_entities", lambda *a, **k: [])
    monkeypatch.setattr(
        photo_flow, "YandexFormsClient", lambda *a, **k: pytest.fail("не должен вызываться")
    )

    cfg = FakePhotoCfg(
        photo_flow_enabled=True,
        dry_run=False,
        force=True,
        photo_theme="животные",
        photo_state_file=str(tmp_path / "photo_state.json"),
    )

    assert photo_flow.run(cfg) == 1


# --- _publish: успешный сценарий -------------------------------------------


class _FakeClient:
    def __init__(self):
        self.uploaded = []
        self.added_images = []
        self.actions = []
        self.deleted = []

    def next_survey_name(self, base):
        return base

    def create_survey(self, name):
        self.actions.append(("create", name))
        return "sid-1"

    def upload_image(self, survey_id, data, filename, content_type="image/jpeg"):
        self.uploaded.append((filename, data, content_type))
        return {"id": f"img-{len(self.uploaded)}", "links": {}, "name": filename}

    def add_enum_question(
        self, survey_id, question, options, correct_index, shuffle=True, image=None
    ):
        self.added_images.append(image)
        return len(self.added_images)

    def update_survey(self, *a, **k):
        self.actions.append("update")

    def set_access(self, *a, **k):
        self.actions.append("access")

    def publish(self, *a, **k):
        self.actions.append("publish")

    def delete_survey(self, *a, **k):
        self.deleted.append(a[0] if a else None)


def _question(entity):
    return {
        "topic": "животные",
        "question": "Что (кто) изображено на фотографии?",
        "options": [entity, "x", "y", "z"],
        "correct_index": 0,
        "theme": "животные",
        "entity": entity,
        "image": {"source": "wikimedia", "url": "https://x/a.jpg"},
        "_image_bytes": b"bytes",
        "_image_mime": "image/jpeg",
    }


class _ClientFactory:
    """Вызываемая замена класса клиента: сохраняет и статический public_url."""

    def __init__(self, client):
        self._client = client

    def __call__(self, *args, **kwargs):
        return self._client

    @staticmethod
    def public_url(survey_id):
        return f"https://forms.yandex.ru/u/{survey_id}/"


def test_publish_uploads_images_and_updates_state(monkeypatch, tmp_path):
    """Успешная публикация: картинки загружены, состояние фото-потока обновлено."""
    client = _FakeClient()
    monkeypatch.setattr(photo_flow, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        photo_flow, "_build_questions", lambda *a, **k: [_question("кот"), _question("пёс")]
    )
    monkeypatch.setattr(photo_flow.publisher, "publish_announcement", lambda *a, **k: ["tg"])

    state_path = tmp_path / "photo_state.json"
    cfg = FakePhotoCfg(
        photo_min_questions=2,
        photo_questions_count=2,
        photo_state_file=str(state_path),
        publish=True,
        number_surveys=False,
    )

    result = photo_flow._publish(cfg, {}, "животные", "Q?", ["кот", "пёс"], 2)

    assert result == 0
    assert len(client.uploaded) == 2
    assert client.added_images == [
        {"id": "img-1", "links": {}, "name": "photo_1.jpg"},
        {"id": "img-2", "links": {}, "name": "photo_2.jpg"},
    ]
    assert ("publish" in client.actions)
    # состояние записано
    assert state_path.exists()
    from state import load_state

    state = load_state(str(state_path))
    assert state["used_themes"] == ["животные"]
    assert state["used_entities"]["животные"] == ["кот", "пёс"]
    assert state["asked_questions"] == ["животные: кот", "животные: пёс"]


def test_publish_deletes_draft_when_minimum_not_reached(monkeypatch, tmp_path):
    """Если в форму добавилось меньше минимума — черновик удаляется, код 1."""
    client = _FakeClient()

    def fail_upload(survey_id, data, filename, content_type="image/jpeg"):
        raise RuntimeError("загрузка не удалась")

    client.upload_image = fail_upload
    monkeypatch.setattr(photo_flow, "YandexFormsClient", lambda *a, **k: client)
    monkeypatch.setattr(
        photo_flow, "_build_questions", lambda *a, **k: [_question("кот"), _question("пёс")]
    )

    state_path = tmp_path / "photo_state.json"
    cfg = FakePhotoCfg(
        photo_min_questions=1,
        photo_questions_count=2,
        photo_state_file=str(state_path),
    )

    result = photo_flow._publish(cfg, {}, "животные", "Q?", ["кот", "пёс"], 2)

    assert result == 1
    assert client.deleted == ["sid-1"]
    assert not state_path.exists()


# --- _dry_run: недобор вопросов --------------------------------------------


# --- дедуп заголовков при нумерации ----------------------------------------


class _NumberingClient:
    """Клиент с суффиксом-нумерацией: next_survey_name добавляет « N»."""

    def __init__(self):
        self.names = []
        self.uploaded = []

    def next_survey_name(self, base):
        return f"{base} {len(self.names) + 2}"

    def create_survey(self, name):
        self.names.append(name)
        return f"sid-{len(self.names)}"

    def upload_image(self, survey_id, data, filename, content_type="image/jpeg"):
        self.uploaded.append(filename)
        return {"id": f"img-{len(self.uploaded)}", "links": {}, "name": filename}

    def add_enum_question(self, survey_id, **kwargs):
        return len(self.uploaded)

    def update_survey(self, *a, **k):
        pass

    def set_access(self, *a, **k):
        pass

    def publish(self, *a, **k):
        pass

    def delete_survey(self, *a, **k):
        pass


class _NumberingFactory:
    def __init__(self, client):
        self._client = client

    def __call__(self, *a, **k):
        return self._client

    @staticmethod
    def public_url(survey_id):
        return f"https://forms.yandex.ru/u/{survey_id}/"


def test_publish_title_dedup_works_with_numbering(monkeypatch, tmp_path):
    """used_titles хранит базовый заголовок: при NUMBER_SURVEYS=true повторов нет."""
    client = _NumberingClient()
    monkeypatch.setattr(photo_flow, "YandexFormsClient", _NumberingFactory(client))
    monkeypatch.setattr(
        photo_flow, "_build_questions", lambda *a, **k: [_question("кот"), _question("пёс")]
    )
    monkeypatch.setattr(photo_flow.publisher, "publish_announcement", lambda *a, **k: [])

    cfg = FakePhotoCfg(
        photo_min_questions=2,
        photo_questions_count=2,
        number_surveys=True,
        publish=False,
        photo_state_file=str(tmp_path / "photo_state.json"),
        photo_title_templates={"generic": ["Вариант А {count}", "Вариант Б {count}"]},
    )

    state = {}
    assert photo_flow._publish(cfg, state, "животные", "Q?", ["кот", "пёс"], 2) == 0
    first_base = state["used_titles"][0]
    first_name = state["published"][0]["title"]
    # в used_titles — базовый заголовок, а имя формы пронумеровано
    assert first_base in {"Вариант А 2", "Вариант Б 2"}
    assert first_name.endswith(" 2")
    assert first_name != first_base

    assert photo_flow._publish(cfg, state, "животные", "Q?", ["кот", "пёс"], 2) == 0
    assert state["published"][1]["title"].startswith(
        "Вариант Б 2" if first_base == "Вариант А 2" else "Вариант А 2"
    )
    assert len(set(state["used_titles"])) == 2


# --- build_survey_name ------------------------------------------------------


def test_build_survey_name_uses_template_with_count():
    """Заголовок строится по шаблону темы и содержит число вопросов."""
    cfg = FakePhotoCfg(
        photo_title_templates={"породы собак": ["Породы собак: {count} фото"]}
    )

    name = photo_flow.build_survey_name(cfg, "породы собак", 10)

    assert name == "Породы собак: 10 фото"


def test_build_survey_name_matches_category_by_regex():
    """Тема сопоставляется с категорией библиотеки по регулярному выражению."""
    cfg = FakePhotoCfg(
        photo_title_templates={"советские фильмы": ["Узнайте фильм. {count} вопросов"]}
    )

    name = photo_flow.build_survey_name(cfg, "советское кино", 8)

    assert name == "Узнайте фильм. 8 вопросов"


def test_build_survey_name_skips_spoiler_templates():
    """Шаблоны со словами из правильных ответов отбрасываются."""
    cfg = FakePhotoCfg(
        photo_title_templates={
            "generic": ["Кто здесь собака? {count}", "Общий заголовок {count}"]
        }
    )

    name = photo_flow.build_survey_name(
        cfg, "животные", 5, correct_answers=["собака"]
    )

    assert name == "Общий заголовок 5"


def test_build_survey_name_avoids_used_titles():
    """Уже использованный заголовок не выбирается, если есть альтернатива."""
    cfg = FakePhotoCfg(
        photo_title_templates={"generic": ["Вариант А {count}", "Вариант Б {count}"]}
    )

    for _ in range(10):
        name = photo_flow.build_survey_name(
            cfg, "животные", 5, used_titles=["Вариант А 5"]
        )
        assert name == "Вариант Б 5"


def test_build_survey_name_falls_back_to_photo_survey_name():
    """Если шаблонов нет — фолбэк на PHOTO_SURVEY_NAME с темой."""
    cfg = FakePhotoCfg(photo_title_templates={}, photo_survey_name="Что на фото")

    name = photo_flow.build_survey_name(cfg, "животные", 10)

    assert name == "Что на фото? Животные"


def test_build_survey_name_fallback_checked_for_spoiler():
    """Фолбэк с темой-спойлером заменяется на базовое PHOTO_SURVEY_NAME."""
    cfg = FakePhotoCfg(photo_title_templates={}, photo_survey_name="Что на фото")

    name = photo_flow.build_survey_name(
        cfg, "животные", 10, correct_answers=["Животные"]
    )

    assert name == "Что на фото"


def test_build_survey_name_skips_template_with_broken_placeholder():
    """Шаблон с неизвестным плейсхолдером пропускается, а не подставляется сырым."""
    cfg = FakePhotoCfg(
        photo_title_templates={"generic": ["Плохой {unknown} {count}", "Хороший {count}"]}
    )

    name = photo_flow.build_survey_name(cfg, "животные", 7)

    assert name == "Хороший 7"


def test_build_survey_name_uses_builtin_library_for_builtin_theme():
    """Встроенная библиотека даёт непустой заголовок для встроенной темы."""
    cfg = FakePhotoCfg(photo_title_templates=None)

    name = photo_flow.build_survey_name(cfg, "советские фильмы", 10)

    assert name
    assert "10" in name


def test_dry_run_counts_only_entities_with_images(monkeypatch):
    """DRY-RUN не должен засчитывать сущности без найденных изображений.

    Не-сухой поток пропускает сущность, если для неё не нашлось картинки
    (`_build_questions` -> `_pick_image`). Значит, план из одних «пустых»
    сущностей должен приводить к коду 1, а не 0.
    """
    monkeypatch.setattr(
        photo_flow,
        "_build_options",
        lambda cfg, theme, entity: ([entity, "b", "c", "d"], 0),
    )
    monkeypatch.setattr(photo_flow, "_search_candidates", lambda cfg, theme, entity: [])

    cfg = FakePhotoCfg(photo_questions_count=1, photo_min_questions=1)
    result = photo_flow._dry_run(cfg, "животные", "Q?", ["кот"], 1)

    assert result == 1


# --- run: защита от повторной публикации в тот же день ----------------------


def test_run_photo_daily_guard_blocks_second_run(monkeypatch, tmp_path):
    """Повторный фото-запуск в тот же день блокируется без сети (код 0)."""
    state_path = tmp_path / "photo_state.json"
    save_state(str(state_path), {"last_publish_date": today_utc()})

    monkeypatch.setattr(
        photo_flow, "YandexFormsClient", lambda *a, **k: pytest.fail("сеть запрещена")
    )
    monkeypatch.setattr(
        photo_flow, "_generate_entities", lambda *a, **k: pytest.fail("LLM запрещена")
    )

    cfg = FakePhotoCfg(
        photo_flow_enabled=True,
        dry_run=False,
        force=False,
        photo_state_file=str(state_path),
    )

    assert photo_flow.run(cfg) == 0


# --- build_survey_name: дополнительные крайние случаи ------------------------


def test_build_survey_name_unknown_theme_uses_generic_library():
    """Неизвестная тема не ломает сборку: берётся generic-библиотека с числом."""
    cfg = FakePhotoCfg(photo_title_templates=None)

    name = photo_flow.build_survey_name(cfg, "неведомая тема xyz", 6)

    assert name
    assert "6" in name


def test_build_survey_name_handles_zero_and_one_count():
    """count=0 и count=1 корректно подставляются в шаблон."""
    cfg = FakePhotoCfg(photo_title_templates={"generic": ["Ровно {count}"]})

    assert photo_flow.build_survey_name(cfg, "животные", 0) == "Ровно 0"
    assert photo_flow.build_survey_name(cfg, "животные", 1) == "Ровно 1"


def test_build_survey_name_all_templates_spoiler_falls_back():
    """Если все шаблоны спойлерят, берётся фолбэк «PHOTO_SURVEY_NAME + тема»."""
    cfg = FakePhotoCfg(
        photo_title_templates={"generic": ["Собака {count}", "Щенок {count}"]},
        photo_survey_name="Что на фото",
    )

    name = photo_flow.build_survey_name(
        cfg, "животные", 5, correct_answers=["собака", "щенок"]
    )

    assert name == "Что на фото? Животные"


def test_build_survey_name_safe_fallback_strips_trailing_question_mark():
    """safe_fallback срезает висячий '?' у PHOTO_SURVEY_NAME.

    Если фолбэк содержит слово из правильного ответа, отдаётся базовое имя
    без вопросительного знака: «Что на фото?» → «Что на фото».
    """
    cfg = FakePhotoCfg(photo_title_templates={}, photo_survey_name="Что на фото?")

    name = photo_flow.build_survey_name(
        cfg, "животные", 10, correct_answers=["животные"]
    )

    assert name == "Что на фото"


def test_publish_title_history_limits_avoidance(monkeypatch, tmp_path):
    """Учитываются только последние PHOTO_TITLE_HISTORY заголовков.

    Заголовок, использованный раньше окна истории, снова доступен; иначе при
    маленьком PHOTO_TITLE_HISTORY выбор был бы заблокирован устаревшими
    записями. Проверяем через `_publish`: в окне только «Вариант Б 2», значит
    выбирается «Вариант А 2».
    """
    client = _FakeClient()
    monkeypatch.setattr(photo_flow, "YandexFormsClient", _ClientFactory(client))
    monkeypatch.setattr(
        photo_flow,
        "_build_questions",
        lambda *a, **k: [_question("кот"), _question("пёс")],
    )
    monkeypatch.setattr(photo_flow.publisher, "publish_announcement", lambda *a, **k: [])

    cfg = FakePhotoCfg(
        photo_min_questions=2,
        photo_questions_count=2,
        number_surveys=False,
        publish=False,
        photo_title_history=1,
        photo_title_templates={"generic": ["Вариант А {count}", "Вариант Б {count}"]},
        photo_state_file=str(tmp_path / "photo_state.json"),
    )
    state = {"used_titles": ["Вариант А 2", "Вариант Б 2"]}

    assert photo_flow._publish(cfg, state, "животные", "Q?", ["кот", "пёс"], 2) == 0
    assert client.actions[0] == ("create", "Вариант А 2")