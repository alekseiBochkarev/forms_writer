"""Точка входа: сгенерировать вопросы и опубликовать их в Яндекс Формах.

Примеры:
    python main.py                      # обычный запуск по настройкам окружения
    python main.py --dry-run            # только показать, что будет создано
    python main.py --questions-file samples/questions.json
    python main.py --count 10 --topic "наука и техника"
    python main.py --check              # проверить доступ к API Яндекс Форм
    python main.py --delete-survey 6aac...            # удалить форму по id
    python main.py --rename-survey 6aac... --name "Новое имя"  # переименовать форму
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, List

from config import load_config
from dedup import DuplicateChecker
from llm import generate_questions
from publisher import publish_announcement
from state import load_state, save_state, today_utc
from yandex_forms import YandexFormsClient, publish_questions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("forms_writer")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Автосоздание тестов в Яндекс Формах")
    parser.add_argument("--dry-run", action="store_true", help="не обращаться к Яндекс Формам")
    parser.add_argument("--questions-file", help="JSON-файл с готовыми вопросами")
    parser.add_argument("--count", type=int, help="сколько вопросов сгенерировать")
    parser.add_argument("--topic", help="тема/направление вопросов")
    parser.add_argument("--survey-id", help="добавить вопросы в существующую форму")
    parser.add_argument("--name", help="название новой формы")
    parser.add_argument("--no-publish", action="store_true", help="не публиковать форму")
    parser.add_argument("--force", action="store_true", help="игнорировать защиту от дублей")
    parser.add_argument("--check", action="store_true", help="проверить доступ к API и выйти")
    parser.add_argument("--delete-survey", help="удалить форму по id и выйти")
    parser.add_argument("--rename-survey", help="переименовать форму по id (вместе с --name)")
    return parser.parse_args()


def load_questions_from_file(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("questions", data) if isinstance(data, dict) else data
    result = []
    for item in items:
        options = item.get("options") or []
        correct = item.get("correct_index")
        if len(options) != 4 or not isinstance(correct, int):
            raise ValueError(f"Некорректный вопрос в файле {path}: {item}")
        result.append(
            {
                "topic": item.get("topic", ""),
                "question": item["question"],
                "options": options,
                "correct_index": correct,
            }
        )
    return result


def _collect_unique_questions(cfg, history: List[str], checker: DuplicateChecker) -> List[Dict[str, Any]]:
    """Сгенерировать вопросы, исключая повторы (в т.ч. по прошлым выпускам)."""
    collected: List[Dict[str, Any]] = []
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        avoid = history + [q["question"] for q in collected]
        batch = generate_questions(cfg, avoid=avoid)
        for q in batch:
            if checker.is_duplicate(q["question"]):
                log.info("  повтор, пропускаю: %s", q["question"])
                continue
            checker.add(q["question"])
            collected.append(q)
        log.info("Попытка %s: уникальных вопросов %s из %s", attempt, len(collected), cfg.count)
        if len(collected) >= cfg.count:
            return collected[: cfg.count]
    raise RuntimeError(
        f"Не удалось собрать {cfg.count} уникальных вопросов за {max_attempts} попыток"
    )


def main() -> int:
    args = parse_args()

    overrides: Dict[str, Any] = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.questions_file:
        overrides["questions_file"] = args.questions_file
    if args.count:
        overrides["count"] = args.count
    if args.topic:
        overrides["topic"] = args.topic
    if args.survey_id:
        overrides["yandex_survey_id"] = args.survey_id
    if args.name:
        overrides["survey_name"] = args.name
    if args.no_publish:
        overrides["publish"] = False
    if args.force:
        overrides["force"] = True

    service_mode = bool(args.check or args.delete_survey or args.rename_survey)
    try:
        cfg = load_config(overrides, require_questions=not service_mode)
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    # Режимы обслуживания: проверка доступа и удаление формы
    if service_mode:
        if cfg.dry_run:
            log.error("Режимы --check/--delete-survey недоступны с --dry-run")
            return 2
        client = YandexFormsClient(cfg.yandex_token, cfg.yandex_org_id, cfg.yandex_org_header)
        if args.check:
            surveys = client.list_surveys()
            log.info("Доступ к API есть. Доступных форм: %s", len(surveys))
            for s in surveys:
                log.info("  %s | %s", s.get("id"), s.get("name"))
            return 0
        if args.rename_survey:
            if not args.name:
                log.error("Для --rename-survey укажите новое имя через --name")
                return 2
            client.update_survey(args.rename_survey, {"name": args.name})
            log.info("Форма %s переименована в «%s»", args.rename_survey, args.name)
            return 0
        client.delete_survey(args.delete_survey)
        log.info("Форма %s удалена", args.delete_survey)
        return 0

    state_data = load_state(cfg.state_file)
    history = list(state_data.get("asked_questions") or [])

    # Защита от повторной публикации в один день (как в article_writer)
    if not cfg.dry_run and not cfg.force:
        if state_data.get("last_publish_date") == today_utc():
            log.info("За %s уже публиковали — пропускаю (используйте --force для повтора).", today_utc())
            return 0

    # Если истории ещё нет — подтягиваем вопросы из уже созданных форм,
    # чтобы не повторять их в новом выпуске.
    if not history and not cfg.dry_run:
        try:
            hist_client = YandexFormsClient(cfg.yandex_token, cfg.yandex_org_id, cfg.yandex_org_header)
            for s in hist_client.list_surveys():
                if not str(s.get("name", "")).strip().startswith(cfg.survey_name):
                    continue
                for q in hist_client.get_questions(s["id"]):
                    if q.get("label"):
                        history.append(q["label"])
            if history:
                log.info("Загружено вопросов из существующих форм: %s", len(history))
                state_data["asked_questions"] = history[-500:]
        except Exception as exc:  # noqa: BLE001 - история не критична для запуска
            log.warning("Не удалось загрузить историю вопросов: %s", exc)

    log.info("Форма: «%s»", cfg.survey_name)
    log.info("Вопросов: %s | тема: %s | публикация: %s", cfg.count, cfg.topic, cfg.publish)

    # 1) Получаем вопросы (без повторов с предыдущими выпусками)
    checker = DuplicateChecker(history)
    if history:
        log.info("Учитываю ранее заданные вопросы: %s", len(history))

    if cfg.questions_file:
        log.info("Читаю вопросы из файла: %s", cfg.questions_file)
        loaded = load_questions_from_file(cfg.questions_file)
        questions = []
        for q in loaded:
            if checker.is_duplicate(q["question"]):
                log.warning("Пропускаю повтор: %s", q["question"])
                continue
            checker.add(q["question"])
            questions.append(q)
    else:
        questions = _collect_unique_questions(cfg, history, checker)

    log.info("Вопросов получено: %s", len(questions))
    for i, q in enumerate(questions, 1):
        correct = q["options"][q["correct_index"]]
        log.info("  %2d. %s  ->  %s", i, q["question"], correct)

    # 2) Dry-run — только показать план
    if cfg.dry_run:
        log.info("DRY-RUN: обращения к Яндекс Формам не будет")
        return 0

    # 3) Создаём и публикуем форму
    client = YandexFormsClient(cfg.yandex_token, cfg.yandex_org_id, cfg.yandex_org_header)
    survey_id = publish_questions(cfg, questions, client)
    public_url = YandexFormsClient.public_url(survey_id)
    log.info("Готово. Публичная ссылка: %s", public_url)

    # 4) Анонс в Telegram и VK
    posted = publish_announcement(cfg, survey_id, len(questions), cfg.survey_name)
    if posted:
        log.info("Анонс опубликован: %s", ", ".join(posted))

    # 5) Сохраняем состояние (защита от дублей и от повторов вопросов)
    state_data["last_publish_date"] = today_utc()
    state_data.setdefault("published", []).append(
        {"date": today_utc(), "survey_id": survey_id, "url": public_url}
    )
    asked = state_data.setdefault("asked_questions", [])
    asked.extend(q["question"] for q in questions)
    state_data["asked_questions"] = asked[-500:]
    save_state(cfg.state_file, state_data)

    return 0


if __name__ == "__main__":
    sys.exit(main())