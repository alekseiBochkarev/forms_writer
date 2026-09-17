"""Клиент Яндекс Форм API: создание формы, добавление вопросов, публикация.

Документация: https://yandex.ru/support/forms/ru/api-ref/
"""

from __future__ import annotations

import json
import logging
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

    def add_enum_question(
        self,
        survey_id: str,
        question: str,
        options: List[str],
        correct_index: int,
        shuffle: bool = True,
    ) -> int:
        payload = {
            "type": "enum",
            "label": question,
            "widget": "radio",
            "has_quiz": True,
            "modify_choices": "shuffle" if shuffle else "natural",
            "items": [
                {
                    "label": option,
                    "correct": (i == correct_index),
                    "scores": (1 if i == correct_index else 0),
                }
                for i, option in enumerate(options)
            ],
        }
        data = self._request("POST", f"/surveys/{survey_id}/questions/", payload)
        return data["id"]

    def publish(self, survey_id: str) -> None:
        self._request("POST", f"/surveys/{survey_id}/publish/")

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

    # Авто-сегменты: 4 диапазона по четвертям от общего числа баллов.
    quiz["calc_method"] = "range"
    limits = [
        max(1, total * 1 // 4),
        max(2, total * 2 // 4),
        max(3, total * 3 // 4),
        total,
    ]
    titles = [
        "Начинающий",
        "Любознательный",
        "Эрудит",
        "Ходячая энциклопедия",
    ]
    quiz["items"] = [
        {
            "title": f"{titles[i]} (0–{limits[i]})",
            "description": "Результат теста на общую эрудицию.",
            "upper_limit": limits[i],
        }
        for i in range(len(limits))
    ]
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
        survey_id = client.create_survey(cfg.survey_name)
        log.info("Создана форма: %s", survey_id)

    for i, q in enumerate(questions, 1):
        qid = client.add_enum_question(
            survey_id,
            question=q["question"],
            options=q["options"],
            correct_index=q["correct_index"],
            shuffle=cfg.shuffle,
        )
        log.info("  + вопрос %s: id=%s [%s]", i, qid, q.get("topic", ""))

    quiz = build_quiz_settings(cfg, total=len(questions))
    client.update_survey(survey_id, {"quiz": quiz, "stats": cfg.stats})
    log.info("Настройки теста применены (stats=%s, quiz=%s)", cfg.stats, quiz.get("calc_method"))

    if cfg.publish:
        client.publish(survey_id)
        log.info("Форма опубликована")

    return survey_id