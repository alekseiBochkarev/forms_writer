"""Клиент Яндекс Форм API: создание формы, добавление вопросов, публикация.

Документация: https://yandex.ru/support/forms/ru/api-ref/
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.forms.yandex.net/v1"

# Ограничение на число страниц при обходе списка форм: страховка от зацикливания.
MAX_SURVEY_PAGES = 1000


class YandexFormsError(RuntimeError):
    pass


def _compose_label(question: str, context: str = "") -> str:
    """Текст вопроса с «завязкой» перед ним (разделитель — пустая строка)."""
    text = str(question or "").strip()
    preface = str(context or "").strip()
    return f"{preface}\n\n{text}" if preface else text


def _image_filename(index: int, mime: str = "") -> str:
    """Имя файла для загрузки изображения вопроса (по mime)."""
    mime = (mime or "").lower()
    if "png" in mime:
        ext = "png"
    elif "webp" in mime:
        ext = "webp"
    else:
        ext = "jpg"
    return f"image_{index}.{ext}"


def build_answer_condition(
    question_slug: str,
    option_slugs: List[str],
    operator: str = "and",
) -> Dict[str, Any]:
    """Условие показа блока: выбран один из `option_slugs`.

    `operator="and"` — один вариант (ровно верный ответ), `"or"` — любой из
    перечисленных (все неверные).
    """
    return {
        "operator": operator,
        "items": [
            {
                "type": "question",
                "condition": "eq",
                "question": question_slug,
                "value": slug,
            }
            for slug in option_slugs
        ],
    }


def add_feedback_blocks(
    client: "YandexFormsClient",
    survey_id: str,
    question: Dict[str, Any],
    correct_index: int,
    cfg,
    explanation: str = "",
) -> None:
    """Добавить после вопроса блоки «Правильно/Ошибочка» с условиями показа.

    `question` — ответ API на создание вопроса с `with_slugs=true`: нужны
    `slug` вопроса и `slug` каждого варианта. `explanation` — пояснение из
    исходного вопроса (в ответе API его нет). Порядок блоков важен: они
    добавляются сразу после вопроса и показываются только при выборе ответа.
    """
    question_slug = str(question.get("slug") or "").strip()
    items = question.get("items") or []
    option_slugs = [str(item.get("slug") or "").strip() for item in items]
    if not question_slug or not option_slugs or not all(option_slugs):
        raise YandexFormsError(
            "API не вернул слаги вопроса/вариантов — нельзя собрать условия показа"
        )

    correct_slug = option_slugs[correct_index]
    wrong_slugs = [slug for i, slug in enumerate(option_slugs) if i != correct_index]
    explanation_text = str(explanation or "").strip()

    correct_text = (
        str(getattr(cfg, "feedback_correct_text", "") or "").strip() or "Правильно."
    )
    wrong_text = (
        str(getattr(cfg, "feedback_wrong_text", "") or "").strip()
        or "Ошибочка. Подумайте ещё."
    )
    if explanation_text:
        correct_text = f"{correct_text} {explanation_text}".strip()

    ok_id = client.add_comment_question(survey_id, correct_text, header=False)
    if not ok_id:
        raise YandexFormsError("API не вернул id блока обратной связи")
    client.add_question_condition(
        survey_id, ok_id, build_answer_condition(question_slug, [correct_slug])
    )
    bad_id = client.add_comment_question(survey_id, wrong_text, header=False)
    if not bad_id:
        raise YandexFormsError("API не вернул id блока обратной связи")
    client.add_question_condition(
        survey_id,
        bad_id,
        build_answer_condition(question_slug, wrong_slugs, operator="or"),
    )


class YandexFormsClient:
    def __init__(self, token: str, org_id: str, org_header: str = "X-Cloud-Org-Id"):
        self.base_url = BASE_URL
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"OAuth {token}",
                org_header: org_id,
                "Content-Type": "application/json",
            }
        )

    # --- низкоуровневый запрос ---
    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        response = self.session.request(
            method, url, json=payload, params=params, timeout=60
        )
        if response.status_code >= 400:
            raise YandexFormsError(
                f"{method} {path} -> {response.status_code}: {response.text[:500]}"
            )
        if not response.text:
            return {}
        return response.json()

    # --- API ---
    def list_surveys(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Получить все доступные формы, обходя страницы API.

        API отдаёт формы страницами (`limit` по умолчанию 10), поэтому без
        обхода `next_survey_name` видел бы только первые 10 форм и выдавал бы
        дубликаты номеров. Ориентируемся только на `links.next`: сервер может
        вернуть меньше запрошенного `limit`, но это ещё не конец списка.
        Останавливаемся, когда пропал `next_link` или страница пуста.
        """
        surveys: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(MAX_SURVEY_PAGES):
            data = self._request(
                "GET", "/surveys/", params={"limit": limit, "offset": offset}
            )
            if not isinstance(data, dict):
                surveys.extend(data or [])
                break
            page = data.get("result") or []
            surveys.extend(page)
            next_link = (data.get("links") or {}).get("next")
            if not page or not next_link:
                break
            offset += len(page)
        else:
            log.warning(
                "Список форм: достигнут предел %s страниц, список может быть неполным",
                MAX_SURVEY_PAGES,
            )
        return surveys

    def next_survey_name(self, base: str) -> str:
        """Имя со счётчиком: «<base> N», где N — число форм с таким префиксом + 1."""
        try:
            surveys = self.list_surveys()
        except YandexFormsError as exc:
            log.warning("Не удалось получить список форм для нумерации: %s", exc)
            return base
        count = sum(
            1 for s in surveys if str(s.get("name", "")).strip().startswith(base)
        )
        return f"{base} {count + 1}"

    def create_survey(self, name: str) -> str:
        payload = {
            "name": name,
            "texts": {
                "submit": "Отправить",
                "title": "Спасибо!",
                "subtitle": "Ваши ответы приняты.",
            },
        }
        data = self._request("POST", "/surveys/", payload)
        return data["id"]

    def get_survey(self, survey_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/surveys/{survey_id}/")

    def update_survey(self, survey_id: str, payload: dict) -> Dict[str, Any]:
        return self._request("PATCH", f"/surveys/{survey_id}/", payload)

    def delete_survey(self, survey_id: str) -> None:
        self._request("DELETE", f"/surveys/{survey_id}/")

    def get_questions(self, survey_id: str) -> List[Dict[str, Any]]:
        data = self._request("GET", f"/surveys/{survey_id}/questions/")
        questions: List[Dict[str, Any]] = []
        for page in data.get("pages", []):
            questions.extend(page.get("items", []))
        return questions

    def delete_question(self, survey_id: str, question_id: int) -> None:
        self._request("DELETE", f"/surveys/{survey_id}/questions/{question_id}/")

    def upload_image(
        self,
        survey_id: str,
        data: bytes,
        filename: str,
        content_type: str = "image/jpeg",
    ) -> Dict[str, Any]:
        """Загрузить изображение для вопроса формы.

        Отправляет multipart/form-data (поле `image`) отдельным запросом: у сессии
        выставлен `Content-Type: application/json`, который для multipart не годится.
        Возвращает только данные изображения для вопроса (`id`, `links`, `name`) —
        без служебных `check_status`/`check_mode`.

        Статус `check` означает, что изображение ещё проверяется антивирусом.
        Отдельной ручки опроса статуса нет, поэтому считаем изображение пригодным,
        но явно предупреждаем, что модерация не подтверждена. При
        `infected`/`error`/`deleted` поднимаем ошибку.
        """
        url = f"{self.base_url}/surveys/{survey_id}/images"
        headers = {
            key: value
            for key, value in self.session.headers.items()
            if key.lower() != "content-type"
        }
        files = {"image": (filename, data, content_type)}
        response = requests.post(url, headers=headers, files=files, timeout=120)
        if response.status_code >= 400:
            raise YandexFormsError(
                f"POST /surveys/{survey_id}/images -> {response.status_code}: "
                f"{response.text[:500]}"
            )
        info: Dict[str, Any] = response.json() if response.text else {}
        status = info.get("check_status")
        if status in ("infected", "error", "deleted"):
            raise YandexFormsError(f"Изображение отклонено API: check_status={status}")
        if status == "check":
            # Короткая пауза: обычно за это время проверка завершается.
            time.sleep(2)
            log.warning(
                "Изображение «%s» ещё проверяется антивирусом (check_status=check); "
                "модерация не подтверждена.",
                info.get("name") or filename,
            )
        return {
            "id": info.get("id"),
            "links": info.get("links"),
            "name": info.get("name"),
        }

    def add_enum_question(
        self,
        survey_id: str,
        question: str,
        options: List[str],
        correct_index: int,
        shuffle: bool = True,
        image: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> int:
        """Добавить вопрос с вариантами и вернуть его id (см. detailed-версию)."""
        data = self.add_enum_question_detailed(
            survey_id,
            question=question,
            options=options,
            correct_index=correct_index,
            shuffle=shuffle,
            image=image,
            context=context,
        )
        return data["id"]

    def add_enum_question_detailed(
        self,
        survey_id: str,
        question: str,
        options: List[str],
        correct_index: int,
        shuffle: bool = True,
        image: Optional[Dict[str, Any]] = None,
        context: str = "",
        has_quiz: bool = True,
        with_slugs: bool = False,
    ) -> Dict[str, Any]:
        """Добавить вопрос и вернуть полный ответ API (слаг вопроса и вариантов).

        Слаги нужны для условий показа (`add_question_condition`): публичный API
        принимает в `question`/`value` именно слаги, а не числовые id. Поэтому в
        режиме обратной связи создаём вопрос с `with_slugs=true`.

        `context` («завязка») ставится в начало `label` через пустую строку —
        один блок текста, как в разобранных каналах.

        `has_quiz=False` — вопрос без подсчёта баллов (режим обратной связи:
        читатель может менять вариант до верного, баллы теряют смысл).
        """
        payload: Dict[str, Any] = {
            "type": "enum",
            "label": _compose_label(question, context),
            "widget": "radio",
            "has_quiz": has_quiz,
            "modify_choices": "shuffle" if shuffle else "natural",
            "validators": [{"type": "required"}],
            "items": [
                {
                    "label": option,
                    **(
                        {
                            "correct": (i == correct_index),
                            "scores": (1 if i == correct_index else 0),
                        }
                        if has_quiz
                        else {}
                    ),
                }
                for i, option in enumerate(options)
            ],
        }
        if image:
            payload["image"] = image
        params = {"with_slugs": "true"} if with_slugs else None
        return self._request(
            "POST", f"/surveys/{survey_id}/questions/", payload, params=params
        )

    def add_question_condition(
        self,
        survey_id: str,
        question_id: int,
        condition: Dict[str, Any],
    ) -> Any:
        """Задать условие показа блока (см. `build_answer_condition`)."""
        return self._request(
            "POST",
            f"/surveys/{survey_id}/questions/{question_id}/conditions",
            condition,
        )

    def add_comment_question(
        self, survey_id: str, label: str, header: bool = False
    ) -> Optional[int]:
        """Добавить блок-комментарий (тип `comment`) в форму.

        Блок не участвует в подсчёте баллов квиза. При `header=True` текст
        отображается как заголовок. Требование API подтверждено по документации
        (QuestionCommentIn): `{"type": "comment", "label": ..., "header": ...}`.
        """
        payload = {"type": "comment", "label": label, "header": header}
        data = self._request("POST", f"/surveys/{survey_id}/questions/", payload)
        return data.get("id")

    def publish(self, survey_id: str) -> None:
        self._request("POST", f"/surveys/{survey_id}/publish/")

    def set_access(self, survey_id: str, access: str = "public", action: str = "submit") -> None:
        """Настроить доступ к форме.

        access: restricted | common | public
        action: change (редактирование) | submit (заполнение)
        """
        self._request(
            "POST",
            f"/surveys/{survey_id}/access",
            {"action": action, "access": access},
        )

    @staticmethod
    def public_url(survey_id: str) -> str:
        return f"https://forms.yandex.ru/u/{survey_id}/"


def build_quiz_settings(cfg, total: int) -> Dict[str, Any]:
    """Собрать блок quiz с учётом настроек.

    Приоритет: SEGMENTS (свои диапазоны) -> авто-сегменты. Сегменты строятся
    для любого теста: способы подсчёта «сегменты» (`range`) и «зачёт/незачёт»
    (`scores` + `pass_scores`) в Яндекс Формах взаимоисключающие. При
    `calc_method="range"` сервер игнорирует `pass_scores` (проверено на боевой
    форме: после сохранения приходит `pass_scores=null`), поэтому порог в quiz
    не передаём.
    """
    quiz: Dict[str, Any] = {
        "show_results": cfg.show_results,
        "show_correct": cfg.show_correct,
    }
    # upper_limit нет в OpenAPI-схеме SurveyQuizItemIn, но API его принимает и
    # возвращает (проверено GET). Без upper_limit сегмент считается верхним.

    if cfg.segments:
        quiz["calc_method"] = "range"
        quiz["items"] = [
            {
                "title": str(seg.get("title", "")),
                "description": str(seg.get("description", "")),
                **({"upper_limit": seg["upper_limit"]} if "upper_limit" in seg else {}),
            }
            for seg in cfg.segments
        ]
        return quiz

    # Авто-сегменты: 4 уровня, границы по четвертям от общего числа баллов.
    quiz["calc_method"] = "range"
    bounds = sorted(
        {
            max(1, total // 4),
            max(2, total // 2),
            max(3, (total * 3) // 4),
            total,
        }
    )
    while bounds and bounds[-1] > total:
        bounds.pop()
    if not bounds:
        bounds = [total]

    levels = [
        ("Начинающий", "Кругозор только формируется — отличный повод узнать больше."),
        ("Любознательный", "Основы есть, но в ряде областей стоит подтянуться."),
        ("Эрудит", "Вы уверенно ориентируетесь в большинстве тем."),
        ("Ходячая энциклопедия", "Отличный результат — по-настоящему широкий кругозор!"),
    ]

    items = []
    lower = 0
    for i, upper in enumerate(bounds):
        name, description = levels[min(i, len(levels) - 1)]
        range_text = str(lower) if lower == upper else f"{lower}–{upper}"
        items.append(
            {
                "title": name,
                "description": f"Верных ответов: {range_text} из {total}. {description}",
                "upper_limit": upper,
            }
        )
        lower = upper + 1

    quiz["items"] = items
    return quiz


def publish_questions(
    cfg,
    questions: List[Dict[str, Any]],
    client: YandexFormsClient,
    intro: str = "",
    outro: str = "",
) -> str:
    """Создать/наполнить форму вопросами и (опционально) опубликовать.

    Вводная добавляется блоком `comment` первым, заключительная — последним
    (блоки комментариев не влияют на баллы квиза). Возвращает id формы.

    При `FEEDBACK_ENABLED` после каждого вопроса добавляются условные блоки
    «Правильно/Ошибочка», а `quiz` в форму не передаётся.
    """
    # Завязки проверяем детерминированно до обращения к API: если контекст
    # раскрывает ответ — убираем его (выпуск не блокируем).
    from review import sanitize_contexts

    sanitize_contexts(questions)

    feedback_mode = bool(getattr(cfg, "feedback_enabled", False))
    with_context = bool(getattr(cfg, "question_context_enabled", False))

    if cfg.yandex_survey_id:
        survey_id = cfg.yandex_survey_id
        log.info("Использую существующую форму: %s", survey_id)
        if cfg.clear_existing:
            existing = client.get_questions(survey_id)
            log.info("Удаляю существующие вопросы: %s", len(existing))
            for q in existing:
                if "id" in q:
                    client.delete_question(survey_id, q["id"])
    else:
        name = cfg.survey_name
        if cfg.number_surveys:
            name = client.next_survey_name(cfg.survey_name)
        survey_id = client.create_survey(name)
        log.info("Создана форма «%s»: %s", name, survey_id)

    if getattr(cfg, "intro_enabled", False) and (intro or "").strip():
        comment_id = client.add_comment_question(survey_id, intro.strip(), header=False)
        log.info("  + вводная часть (comment id=%s)", comment_id)

    for i, q in enumerate(questions, 1):
        context = str(q.get("context") or "").strip() if with_context else ""
        image = q.get("image")
        # Вопросы из файла могут нести байты картинки (`_image_bytes`): загружаем
        # их в форму и передаём ссылку на изображение в вопрос.
        if q.get("_image_bytes"):
            try:
                image = client.upload_image(
                    survey_id,
                    q["_image_bytes"],
                    _image_filename(i, q.get("_image_mime") or ""),
                    q.get("_image_mime") or "image/jpeg",
                )
                log.info("  + изображение к вопросу %s", i)
            except Exception as exc:  # noqa: BLE001 - вопрос добавим без картинки
                log.warning(
                    "Не удалось загрузить изображение к вопросу «%s»: %s",
                    q["question"],
                    exc,
                )
                image = None
            finally:
                q.pop("_image_bytes", None)
                q.pop("_image_mime", None)
        if feedback_mode:
            # В режиме обратной связи нужны слаги вопроса и вариантов — по ним
            # строятся условия показа блоков «Правильно/Ошибочка».
            data = client.add_enum_question_detailed(
                survey_id,
                question=q["question"],
                options=q["options"],
                correct_index=q["correct_index"],
                shuffle=cfg.shuffle,
                image=image,
                context=context,
                has_quiz=False,
                with_slugs=True,
            )
            qid = data.get("id")
            add_feedback_blocks(
                client,
                survey_id,
                data,
                q["correct_index"],
                cfg,
                explanation=str(q.get("explanation") or ""),
            )
            log.info(
                "  + вопрос %s: id=%s [%s] + обратная связь",
                i,
                qid,
                q.get("topic", ""),
            )
        else:
            qid = client.add_enum_question(
                survey_id,
                question=q["question"],
                options=q["options"],
                correct_index=q["correct_index"],
                shuffle=cfg.shuffle,
                image=image,
                context=context,
            )
            log.info("  + вопрос %s: id=%s [%s]", i, qid, q.get("topic", ""))

    if getattr(cfg, "conclusion_enabled", False) and (outro or "").strip():
        comment_id = client.add_comment_question(survey_id, outro.strip(), header=False)
        log.info("  + заключительная часть (comment id=%s)", comment_id)

    if feedback_mode:
        # Баллы и сегменты в этом режиме не имеют смысла: читатель может менять
        # вариант до верного, а пояснение показывается сразу.
        client.update_survey(survey_id, {"stats": cfg.stats, "need_auth": False})
        log.info("Режим обратной связи: квиз не настраивается (stats=%s)", cfg.stats)
    else:
        quiz = build_quiz_settings(cfg, total=len(questions))
        client.update_survey(
            survey_id, {"quiz": quiz, "stats": cfg.stats, "need_auth": False}
        )
        log.info(
            "Настройки теста применены (stats=%s, quiz=%s)",
            cfg.stats,
            quiz.get("calc_method"),
        )

    # Открываем публичный доступ: иначе обычные пользователи не откроют ссылку.
    client.set_access(survey_id, access="public", action="submit")
    log.info("Доступ к форме: публичный (заполнение)")

    if cfg.publish:
        client.publish(survey_id)
        log.info("Форма опубликована")

    return survey_id