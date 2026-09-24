"""Тесты photo_flow.py: формулировки, выбор темы, сборка опций и запуск."""

from __future__ import annotations

import photo_flow
import pytest
from helpers import FakePhotoCfg
from photo_sources import ImageCandidate


# --- question_text ----------------------------------------------------------


def test_question_text_film_theme():
    """Темы про фильмы/кино -> «Кадр из какого фильма?»."""
    assert photo_flow.question_text("советские фильмы") == "Кадр из какого фильма?"
    assert photo_flow.question_text("иностранные фильмы") == "Кадр из какого фильма?"
    assert photo_flow.question_text("кино") == "Кадр из какого фильма?"


def test_question_text_actor_theme():
    """Темы про актёров -> «Кто на фото?»."""
    assert photo_flow.question_text("актёры") == "Кто на фото?"
    assert photo_flow.question_text("актеры") == "Кто на фото?"


def test_question_text_other_theme():
    """Прочие темы -> общая формулировка."""
    assert (
        photo_flow.question_text("животные")
        == "Что (кто) изображено на фотографии?"
    )


def test_image_query_for_film_prepends_film():
    """Для кинотемы поисковый запрос дополняется словом film."""
    assert photo_flow.image_query("советские фильмы", "Иван Васильевич") == (
        "film Иван Васильевич"
    )
    assert photo_flow.image_query("животные", "кот") == "кот"


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

    monkeypatch.setattr(photo_flow.requests, "post", forbidden)
    monkeypatch.setattr(photo_flow, "YandexFormsClient", forbidden)

    cfg = FakePhotoCfg(photo_flow_enabled=False)
    assert photo_flow.run(cfg) == 1


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