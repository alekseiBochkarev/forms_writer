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


class YandexFormsError(RuntimeError):
    pass


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
    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        response = self.session.request(method, url, json=payload, timeout=60)
        if response.status_code >= 400:
            raise YandexFormsError(
                f"{method} {path} -> {response.status_code}: {response.text[:500]}"
            )
        if not response.text:
            return {}
        return response.json()

    # --- API ---
    def list_surveys(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/surveys/")
        return data.get("result", []) if isinstance(data, dict) else data

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
    ) -> int:
        payload = {
            "type": "enum",
            "label": question,
            "widget": "radio",
            "has_quiz": True,
            "modify_choices": "shuffle" if shuffle else "natural",
            "validators": [{"type": "required"}],
            "items": [
                {
                    "label": option,
                    "correct": (i == correct_index),
                    "scores": (1 if i == correct_index else 0),
                }
                for i, option in enumerate(options)
            ],
        }
        if image:
            payload["image"] = image
        data = self._request("POST", f"/surveys/{survey_id}/questions/", payload)
        return data["id"]

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

    Приоритет: SEQUENCES (диапазоны) -> PASS_SCORES (порог) -> авто-сегменты.
    """
    quiz: Dict[str, Any] = {
        "show_results": cfg.show_results,
        "show_correct": cfg.show_correct,
    }

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
        if cfg.pass_scores is not None:
            quiz["pass_scores"] = cfg.pass_scores
        return quiz

    if cfg.pass_scores is not None:
        quiz["calc_method"] = "scores"
        quiz["pass_scores"] = cfg.pass_scores
        quiz["items"] = [
            {
                "title": "Тест пройден" if cfg.pass_scores else "",
                "description": "Спасибо за прохождение теста!",
            }
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


def publish_questions(cfg, questions: List[Dict[str, Any]], client: YandexFormsClient) -> str:
    """Создать/наполнить форму вопросами и (опционально) опубликовать.

    Возвращает id формы.
    """
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

    for i, q in enumerate(questions, 1):
        qid = client.add_enum_question(
            survey_id,
            question=q["question"],
            options=q["options"],
            correct_index=q["correct_index"],
            shuffle=cfg.shuffle,
            image=q.get("image"),
        )
        log.info("  + вопрос %s: id=%s [%s]", i, qid, q.get("topic", ""))

    quiz = build_quiz_settings(cfg, total=len(questions))
    client.update_survey(
        survey_id, {"quiz": quiz, "stats": cfg.stats, "need_auth": False}
    )
    log.info("Настройки теста применены (stats=%s, quiz=%s)", cfg.stats, quiz.get("calc_method"))

    # Открываем публичный доступ: иначе обычные пользователи не откроют ссылку.
    client.set_access(survey_id, access="public", action="submit")
    log.info("Доступ к форме: публичный (заполнение)")

    if cfg.publish:
        client.publish(survey_id)
        log.info("Форма опубликована")

    return survey_id