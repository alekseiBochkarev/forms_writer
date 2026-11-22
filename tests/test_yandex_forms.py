"""Тесты yandex_forms.py: image в payload и multipart-загрузка (сеть замокана)."""

from __future__ import annotations

import pytest

import yandex_forms
from helpers import FakeResponse
from yandex_forms import (
    MAX_SURVEY_PAGES,
    YandexFormsClient,
    YandexFormsError,
    build_quiz_settings,
    publish_questions,
)


# --- add_enum_question -----------------------------------------------------


def _client_with_captured_request():
    client = YandexFormsClient("token", "org")
    captured = {}

    def fake_request(method, path, payload=None, params=None):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        captured["params"] = params
        return {"id": 777}

    client._request = fake_request  # type: ignore[method-assign]
    return client, captured


def test_add_enum_question_without_image_has_no_image_key():
    """Без `image` ключа `image` в payload нет."""
    client, captured = _client_with_captured_request()

    qid = client.add_enum_question("s1", "Q?", ["a", "b", "c", "d"], 1)

    assert qid == 777
    assert "image" not in captured["payload"]
    assert captured["payload"]["type"] == "enum"


def test_add_enum_question_with_image_includes_it():
    """С `image` payload содержит ровно переданный словарь."""
    client, captured = _client_with_captured_request()
    image = {"id": "img-1", "links": {"original": "https://x/img.jpg"}, "name": "img.jpg"}

    client.add_enum_question("s1", "Q?", ["a", "b", "c", "d"], 0, image=image)

    assert captured["payload"]["image"] == image


def test_add_enum_question_marks_only_correct_item():
    """Правильный вариант один: correct True и scores=1."""
    client, captured = _client_with_captured_request()

    client.add_enum_question("s1", "Q?", ["a", "b", "c", "d"], 2)

    items = captured["payload"]["items"]
    assert [it["correct"] for it in items] == [False, False, True, False]
    assert [it["scores"] for it in items] == [0, 0, 1, 0]


# --- add_comment_question ---------------------------------------------------


def test_add_comment_question_payload():
    """Блок-комментарий отправляется с типом comment и флагом header."""
    client, captured = _client_with_captured_request()

    client.add_comment_question("s1", "Вводный текст", header=True)

    assert captured["payload"] == {
        "type": "comment",
        "label": "Вводный текст",
        "header": True,
    }


# --- publish_questions: вводная и заключительная ----------------------------


class _CommentClient:
    def __init__(self):
        self.comments = []
        self.questions = []
        self.updates = []

    def create_survey(self, name):
        return "sid-1"

    def next_survey_name(self, name):
        return name

    def add_comment_question(self, survey_id, label, header=False):
        self.comments.append((label, header))
        return len(self.comments)

    def add_enum_question(self, survey_id, **kwargs):
        self.questions.append(kwargs)
        return len(self.questions)

    def update_survey(self, survey_id, payload):
        self.updates.append(payload)

    def set_access(self, *a, **k):
        pass

    def publish(self, *a, **k):
        pass


def _publish_cfg(**overrides):
    import types

    base = {
        "yandex_survey_id": None,
        "clear_existing": False,
        "survey_name": "Тест",
        "number_surveys": False,
        "shuffle": True,
        "stats": True,
        "publish": False,
        "intro_enabled": True,
        "conclusion_enabled": True,
        "show_results": True,
        "show_correct": True,
        "segments": None,
        "pass_scores": None,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_publish_questions_adds_intro_first_and_outro_last():
    """Вводная — первым блоком, заключительная — последним."""
    client = _CommentClient()
    question = {
        "topic": "t",
        "question": "Q?",
        "options": ["a", "b", "c", "d"],
        "correct_index": 0,
    }

    publish_questions(
        _publish_cfg(), [question], client, intro="Вводная", outro="Заключительная"
    )

    assert client.comments == [("Вводная", False), ("Заключительная", False)]
    assert len(client.questions) == 1


def test_publish_questions_skips_disabled_comments():
    """При выключенных флагах comment-блоки не добавляются."""
    client = _CommentClient()
    question = {
        "topic": "t",
        "question": "Q?",
        "options": ["a", "b", "c", "d"],
        "correct_index": 0,
    }

    publish_questions(
        _publish_cfg(intro_enabled=False, conclusion_enabled=False),
        [question],
        client,
        intro="Вводная",
        outro="Заключительная",
    )

    assert client.comments == []


def test_publish_questions_quiz_total_counts_only_enum_questions():
    """total квиза = число enum-вопросов; intro/outro (comment) не учитываются.

    Иначе авто-сегменты квиза строились бы по завышенному знаменателю и
    результат прохождения считался бы неверно.
    """
    client = _CommentClient()
    questions = [
        {"topic": "t", "question": "Q1?", "options": ["a", "b", "c", "d"], "correct_index": 0},
        {"topic": "t", "question": "Q2?", "options": ["a", "b", "c", "d"], "correct_index": 1},
    ]

    publish_questions(
        _publish_cfg(), questions, client, intro="Вводная", outro="Заключительная"
    )

    quiz = client.updates[-1]["quiz"]
    # 2 enum-вопроса + 2 comment-блока -> total должен быть 2, а не 4
    assert quiz["items"][-1]["upper_limit"] == 2
    assert "из 2." in quiz["items"][-1]["description"]


# --- build_quiz_settings: сегменты ------------------------------------------


def test_quiz_auto_segments_for_13_questions():
    """Без настроек — 4 сегмента по четвертям и полное покрытие баллов."""
    quiz = build_quiz_settings(_publish_cfg(), total=13)

    assert quiz["calc_method"] == "range"
    assert [item["upper_limit"] for item in quiz["items"]] == [3, 6, 9, 13]
    assert quiz["items"][0]["title"] == "Начинающий"
    assert quiz["items"][-1]["title"] == "Ходячая энциклопедия"
    assert "0–3" in quiz["items"][0]["description"]
    assert "10–13" in quiz["items"][-1]["description"]


def test_quiz_pass_scores_do_not_replace_segments():
    """Порог не переключает quiz на scores и не попадает в range-payload."""
    quiz = build_quiz_settings(_publish_cfg(pass_scores=10), total=13)

    assert quiz["calc_method"] == "range"
    assert "pass_scores" not in quiz
    assert len(quiz["items"]) == 4
    assert quiz["items"][-1]["upper_limit"] == 13


def test_quiz_custom_segments_take_priority():
    """Свои SEGMENTS важнее авто-сегментов; режим остаётся range."""
    quiz = build_quiz_settings(
        _publish_cfg(
            pass_scores=10,
            segments=[
                {"title": "Новичок", "description": "Есть куда расти", "upper_limit": 4},
                {"title": "Эрудит", "description": "Отличный результат", "upper_limit": 13},
            ],
        ),
        total=13,
    )

    assert quiz["calc_method"] == "range"
    assert "pass_scores" not in quiz
    assert [item["upper_limit"] for item in quiz["items"]] == [4, 13]


@pytest.mark.parametrize(
    "total, expected",
    [(1, [1]), (2, [1, 2]), (3, [1, 2, 3]), (4, [1, 2, 3, 4])],
)
def test_quiz_auto_segments_for_small_totals(total, expected):
    """На малых total границы не дублируются и покрывают все баллы."""
    quiz = build_quiz_settings(_publish_cfg(), total=total)

    assert [item["upper_limit"] for item in quiz["items"]] == expected
    assert quiz["items"][-1]["upper_limit"] == total


# --- upload_image -----------------------------------------------------------


def _mock_post(monkeypatch, info, status_code=200):
    captured = {}

    def fake_post(url, headers=None, files=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["files"] = files
        return FakeResponse(status_code=status_code, json_data=info)

    monkeypatch.setattr(yandex_forms.requests, "post", fake_post)
    return captured


def test_upload_image_ready_returns_only_public_fields(monkeypatch):
    """check_status=ready: возвращаются только id/links/name, служебные поля убраны."""
    captured = _mock_post(
        monkeypatch,
        {
            "id": "img-1",
            "links": {"original": "https://x/img.jpg"},
            "name": "img.jpg",
            "check_status": "ready",
            "check_mode": "antivirus",
        },
    )
    client = YandexFormsClient("token", "org")

    result = client.upload_image("s1", b"bytes", "img.jpg", "image/jpeg")

    assert set(result.keys()) == {"id", "links", "name"}
    assert result == {
        "id": "img-1",
        "links": {"original": "https://x/img.jpg"},
        "name": "img.jpg",
    }
    assert captured["url"].endswith("/surveys/s1/images")
    assert captured["files"]["image"] == ("img.jpg", b"bytes", "image/jpeg")


def test_upload_image_sends_no_json_content_type(monkeypatch):
    """multipart-запрос НЕ шлёт Content-Type: application/json в заголовках."""
    captured = _mock_post(monkeypatch, {"id": "i", "links": {}, "name": "n", "check_status": "ready"})
    client = YandexFormsClient("token", "org")

    client.upload_image("s1", b"bytes", "img.jpg", "image/jpeg")

    lowered = {k.lower(): v for k, v in captured["headers"].items()}
    assert "content-type" not in lowered
    # авторизация и орг-заголовок при этом сохранены
    assert lowered.get("authorization") == "OAuth token"
    assert lowered.get("x-cloud-org-id") == "org"


@pytest.mark.parametrize("status", ["infected", "error", "deleted"])
def test_upload_image_rejected_status_raises(monkeypatch, status):
    """check_status infected/error/deleted — YandexFormsError."""
    _mock_post(monkeypatch, {"id": "i", "check_status": status})
    client = YandexFormsClient("token", "org")

    with pytest.raises(YandexFormsError) as exc:
        client.upload_image("s1", b"bytes", "img.jpg", "image/jpeg")

    assert status in str(exc.value)


def test_upload_image_http_error_raises(monkeypatch):
    """HTTP >= 400 — YandexFormsError."""
    _mock_post(monkeypatch, {"error": "bad"}, status_code=500)
    client = YandexFormsClient("token", "org")

    with pytest.raises(YandexFormsError):
        client.upload_image("s1", b"bytes", "img.jpg", "image/jpeg")


# --- list_surveys: пагинация -------------------------------------------------


def _paged_client(all_surveys):
    """Клиент с замоканным `_request`, отдающий формы страницами по `limit`."""
    client = YandexFormsClient("token", "org")
    calls = []

    def fake_request(method, path, payload=None, params=None):
        calls.append(params or {})
        offset = (params or {}).get("offset", 0)
        limit = (params or {}).get("limit", 10)
        page = all_surveys[offset : offset + limit]
        has_next = offset + limit < len(all_surveys)
        links = {"next": "https://api.forms.yandex.net/v1/surveys/?offset=x"} if has_next else {}
        return {"result": page, "links": links}

    client._request = fake_request  # type: ignore[method-assign]
    return client, calls


def test_list_surveys_collects_all_pages():
    """3 страницы по 100/100/50 → 250 форм, offset/limit переданы корректно."""
    all_surveys = [{"id": f"id{i}", "name": f"Форма {i}"} for i in range(250)]
    client, calls = _paged_client(all_surveys)

    result = client.list_surveys()

    assert len(result) == 250
    assert result[0] == all_surveys[0]
    assert result[-1] == all_surveys[-1]
    assert [c["offset"] for c in calls] == [0, 100, 200]
    assert all(c["limit"] == 100 for c in calls)


def test_list_surveys_stops_without_next_link():
    """Полная страница без `links.next` — обход завершается на ней."""
    all_surveys = [{"id": f"id{i}", "name": f"Форма {i}"} for i in range(100)]
    client = YandexFormsClient("token", "org")
    calls = []

    def fake_request(method, path, payload=None, params=None):
        calls.append(params or {})
        return {"result": all_surveys, "links": {}}

    client._request = fake_request  # type: ignore[method-assign]

    result = client.list_surveys()

    assert len(result) == 100
    assert len(calls) == 1


def test_list_surveys_stops_on_empty_page():
    """Пустая страница завершает обход, даже если `links.next` присутствует."""
    client = YandexFormsClient("token", "org")

    def fake_request(method, path, payload=None, params=None):
        return {"result": [], "links": {"next": "https://x/next"}}

    client._request = fake_request  # type: ignore[method-assign]

    assert client.list_surveys() == []


def test_list_surveys_does_not_loop_forever_on_repeating_next():
    """Повторяющиеся `offset`/`links.next` не приводят к бесконечному циклу."""
    client = YandexFormsClient("token", "org")
    calls = []

    def fake_request(method, path, payload=None, params=None):
        calls.append(params or {})
        page = [{"id": f"id{i}", "name": "Форма"} for i in range(100)]
        return {"result": page, "links": {"next": "https://x/next"}}

    client._request = fake_request  # type: ignore[method-assign]

    result = client.list_surveys()

    assert len(calls) == MAX_SURVEY_PAGES
    assert len(result) == MAX_SURVEY_PAGES * 100


def test_next_survey_name_counts_forms_beyond_first_page():
    """Форма «… 9» на 10-й+ странице учитывается: возвращается «… 10»."""
    base = "Насколько широк ваш кругозор"
    all_surveys = [{"id": f"id{i}", "name": "Другое"} for i in range(950)]
    all_surveys.extend(
        {"id": f"base{i}", "name": f"{base} {i}"} for i in range(1, 10)
    )
    client, calls = _paged_client(all_surveys)

    name = client.next_survey_name(base)

    assert name == f"{base} 10"
    assert len(calls) > 1


def _capped_client(all_surveys, page_size=10):
    """Сервер ограничивает `limit` сверху и отдаёт ровно `page_size` форм."""
    client = YandexFormsClient("token", "org")
    calls = []

    def fake_request(method, path, payload=None, params=None):
        calls.append(params or {})
        offset = (params or {}).get("offset", 0)
        page = all_surveys[offset : offset + page_size]
        has_next = offset + page_size < len(all_surveys)
        links = {"next": "https://api.forms.yandex.net/v1/surveys/?offset=x"} if has_next else {}
        return {"result": page, "links": links}

    client._request = fake_request  # type: ignore[method-assign]
    return client, calls


def test_list_surveys_handles_server_capped_limit():
    """Запрошен limit=100, но сервер отдаёт по 10: собираются все 3×10=30 форм."""
    all_surveys = [{"id": f"id{i}", "name": f"Форма {i}"} for i in range(30)]
    client, calls = _capped_client(all_surveys)

    result = client.list_surveys(limit=100)

    assert len(result) == 30
    assert [c["offset"] for c in calls] == [0, 10, 20]
    assert all(c["limit"] == 100 for c in calls)


def test_next_survey_name_sees_form_on_third_capped_page():
    """При серверном лимите 10 форма с 3-й страницы учитывается в нумерации."""
    base = "Насколько широк ваш кругозор"
    all_surveys = [{"id": f"id{i}", "name": "Другое"} for i in range(20)]
    all_surveys.extend(
        {"id": f"base{i}", "name": f"{base} {i}"} for i in range(1, 10)
    )
    client, calls = _capped_client(all_surveys)

    name = client.next_survey_name(base)

    assert name == f"{base} 10"
    assert len(calls) == 3


# --- завязка и режим обратной связи ----------------------------------------


def test_compose_label_puts_context_before_question():
    """Завязка идёт перед вопросом через пустую строку."""
    assert (
        yandex_forms._compose_label("Вопрос?", "Короткая завязка.")
        == "Короткая завязка.\n\nВопрос?"
    )


def test_compose_label_without_context_is_plain_question():
    assert yandex_forms._compose_label("Вопрос?", "") == "Вопрос?"
    assert yandex_forms._compose_label("Вопрос?") == "Вопрос?"


def test_compose_label_strips_whitespace():
    assert (
        yandex_forms._compose_label("  Вопрос?  ", "  Завязка.  ")
        == "Завязка.\n\nВопрос?"
    )


def test_add_enum_question_sends_context_in_label():
    """Клиент кладёт завязку в label, не в comment."""
    client, captured = _client_with_captured_request()

    client.add_enum_question(
        "s1", "Q?", ["a", "b", "c", "d"], 1, context="Завязка."
    )

    assert captured["payload"]["label"] == "Завязка.\n\nQ?"
    assert "comment" not in captured["payload"]


def test_build_answer_condition_single_option():
    """Условие «верный ответ»: operator=and и один eq по слагу варианта."""
    condition = yandex_forms.build_answer_condition("id-enum-1", ["777"])

    assert condition == {
        "operator": "and",
        "items": [
            {"type": "question", "condition": "eq", "question": "id-enum-1", "value": "777"}
        ],
    }


def test_build_answer_condition_any_wrong_option():
    """Условие «любой из неверных»: operator=or и по eq на каждый вариант."""
    condition = yandex_forms.build_answer_condition(
        "id-enum-1", ["1", "2", "3"], operator="or"
    )

    assert condition["operator"] == "or"
    assert [item["value"] for item in condition["items"]] == ["1", "2", "3"]


class _FeedbackClient:
    """Заглушка клиента: отдаёт слаги и запоминает условия показа."""

    def __init__(self):
        self.comments = []
        self.questions = []
        self.conditions = []
        self.updates = []
        self.order = []

    def create_survey(self, name):
        return "sid-1"

    def next_survey_name(self, name):
        return name

    def add_enum_question_detailed(self, survey_id, **kwargs):
        self.questions.append(kwargs)
        number = len(self.questions)
        self.order.append(f"q{number}")
        return {
            "id": 100 + number,
            "slug": f"id-enum-{100 + number}",
            "items": [
                {"id": i, "slug": f"slug-{number}-{i}"}
                for i in range(len(kwargs["options"]))
            ],
        }

    def add_comment_question(self, survey_id, label, header=False):
        self.comments.append((label, header))
        self.order.append(f"c{len(self.comments)}")
        return len(self.comments)

    def add_question_condition(self, survey_id, question_id, condition):
        self.conditions.append((question_id, condition))

    def update_survey(self, survey_id, payload):
        self.updates.append(payload)

    def set_access(self, *a, **k):
        pass

    def publish(self, *a, **k):
        pass


def _feedback_cfg(**overrides):
    return _publish_cfg(
        question_context_enabled=True,
        feedback_enabled=True,
        feedback_correct_text="Правильно.",
        feedback_wrong_text="Ошибочка. Подумайте ещё.",
        **overrides,
    )


def _one_question(**overrides):
    question = {
        "topic": "t",
        "question": "Q?",
        "options": ["a", "b", "c", "d"],
        "correct_index": 1,
        "context": "Завязка.",
        "explanation": "Потому что так верно.",
    }
    question.update(overrides)
    return question


def test_publish_questions_feedback_mode_adds_two_conditional_blocks():
    """После вопроса — два блока: верный и любой неверный вариант."""
    client = _FeedbackClient()

    publish_questions(_feedback_cfg(), [_one_question()], client)

    assert [label for label, _ in client.comments] == [
        "Правильно. Потому что так верно.",
        "Ошибочка. Подумайте ещё.",
    ]
    assert len(client.conditions) == 2

    ok_id, ok_condition = client.conditions[0]
    bad_id, bad_condition = client.conditions[1]
    assert ok_id == 1 and bad_id == 2
    assert ok_condition["operator"] == "and"
    assert [item["value"] for item in ok_condition["items"]] == ["slug-1-1"]
    assert bad_condition["operator"] == "or"
    assert [item["value"] for item in bad_condition["items"]] == [
        "slug-1-0",
        "slug-1-2",
        "slug-1-3",
    ]
    assert {item["question"] for item in ok_condition["items"]} == {"id-enum-101"}


def test_publish_questions_feedback_mode_keeps_block_order():
    """Порядок: вводная → вопрос → верно → неверно → заключительная."""
    client = _FeedbackClient()

    publish_questions(
        _feedback_cfg(),
        [_one_question()],
        client,
        intro="Вводная",
        outro="Заключительная",
    )

    assert client.order == ["c1", "q1", "c2", "c3", "c4"]


def test_publish_questions_feedback_mode_disables_quiz():
    """В режиме обратной связи квиз не настраивается (баллы не считаются)."""
    client = _FeedbackClient()

    publish_questions(_feedback_cfg(), [_one_question()], client)

    assert "quiz" not in client.updates[-1]
    assert client.questions[0]["has_quiz"] is False


def test_publish_questions_feedback_mode_requires_slugs():
    """Вопрос создаётся с with_slugs, иначе условия показа не собрать."""
    client = _FeedbackClient()

    publish_questions(_feedback_cfg(), [_one_question()], client)

    assert client.questions[0]["with_slugs"] is True
    assert client.questions[0]["context"] == "Завязка."


def test_publish_questions_feedback_mode_without_explanation_keeps_text():
    """Без пояснения блок верного ответа — только заголовок."""
    client = _FeedbackClient()

    publish_questions(
        _feedback_cfg(), [_one_question(explanation="")], client
    )

    assert client.comments[0][0] == "Правильно."


def test_publish_questions_feedback_failure_raises(monkeypatch):
    """Если условие показа не создалось — публикация падает (fail-closed)."""

    class _BrokenConditionClient(_FeedbackClient):
        def add_question_condition(self, *a, **k):
            raise YandexFormsError("conditions not supported")

    client = _BrokenConditionClient()
    client.publish = lambda *a, **k: pytest.fail("форма не должна публиковаться")

    with pytest.raises(YandexFormsError):
        publish_questions(_feedback_cfg(), [_one_question()], client)


def test_publish_questions_without_slugs_raises_yandex_error():
    """Пустой ответ API без слагов — ошибка, а не форма с вечным блоком."""

    class _NoSlugClient(_FeedbackClient):
        def add_enum_question_detailed(self, survey_id, **kwargs):
            return {"id": 1, "items": [{"id": i} for i in range(4)]}

    with pytest.raises(YandexFormsError):
        publish_questions(_feedback_cfg(), [_one_question()], _NoSlugClient())


def test_publish_questions_clears_context_that_spoils_answer():
    """Завязка-спойлер убирается перед созданием вопроса."""
    client = _CommentClient()
    question = _one_question(
        question="Кто написал «Войну и мир»?",
        options=["Толстой", "Достоевский", "Чехов", "Тургенев"],
        correct_index=0,
        context="Этот Толстой любил простую жизнь.",
        explanation="",
    )

    publish_questions(_publish_cfg(), [question], client)

    assert client.questions[0]["context"] == ""


def test_publish_questions_skips_context_when_disabled():
    """При QUESTION_CONTEXT_ENABLED=false завязка не выводится."""
    client = _CommentClient()

    publish_questions(
        _publish_cfg(question_context_enabled=False), [_one_question()], client
    )

    assert client.questions[0]["context"] == ""