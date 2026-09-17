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

    # Защита от повторной публикации в один день (как в article_writer)
    if not cfg.dry_run and not cfg.force:
        current = load_state(cfg.state_file)
        if current.get("last_publish_date") == today_utc():
            log.info("За %s уже публиковали — пропускаю (используйте --force для повтора).", today_utc())
            return 0

    log.info("Форма: «%s»", cfg.survey_name)
    log.info("Вопросов: %s | тема: %s | публикация: %s", cfg.count, cfg.topic, cfg.publish)

    # 1) Получаем вопросы
    if cfg.questions_file:
        log.info("Читаю вопросы из файла: %s", cfg.questions_file)
        questions = load_questions_from_file(cfg.questions_file)
    else:
        questions = generate_questions(cfg)

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

    # 5) Сохраняем состояние (защита от дублей)
    current = load_state(cfg.state_file)
    current["last_publish_date"] = today_utc()
    current.setdefault("published", []).append(
        {"date": today_utc(), "survey_id": survey_id, "url": public_url}
    )
    save_state(cfg.state_file, current)

    return 0


if __name__ == "__main__":
    sys.exit(main())