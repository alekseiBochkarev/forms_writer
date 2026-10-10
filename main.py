"""Точка входа: сгенерировать вопросы и опубликовать их в Яндекс Формах.

Примеры:
    python main.py                      # обычный запуск по настройкам окружения
    python main.py --dry-run            # только показать, что будет создано
    python main.py --questions-file samples/questions.json
    python main.py --count 10 --topic "наука и техника"
    python main.py --check              # проверить доступ к API Яндекс Форм
    python main.py --delete-survey 6aac...            # удалить форму по id
    python main.py --rename-survey 6aac... --name "Новое имя"  # переименовать форму
    python main.py --photo               # выпуск фото-теста (одна тема)
    python main.py --photo --photo-theme "советские фильмы"
    python main.py --new-tests           # поток «новых тестов» (канал @qa_helper_draft)
    python main.py --new-tests --dry-run # план нового теста без обращения к API
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from typing import Any, Dict, List, Tuple

import photo_flow
import review
from config import load_config
from dedup import DuplicateChecker
from llm import generate_conclusion, generate_intro, generate_questions
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
    parser.add_argument("--photo", action="store_true", help="создать тест с фотографиями")
    parser.add_argument(
        "--photo-theme", help="тема фото-теста (иначе выбирается случайная)"
    )
    parser.add_argument(
        "--new-tests",
        action="store_true",
        help="запустить отдельный поток «новых тестов» (канал @qa_helper_draft)",
    )
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
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        avoid = history + [q["question"] for q in collected]
        try:
            batch = generate_questions(cfg, avoid=avoid)
        except Exception as exc:  # noqa: BLE001 - транзиентный сбой одной попытки
            log.warning("Попытка %s: не удалось получить вопросы (%s)", attempt, exc)
            continue
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


def _topics_of(questions: List[Dict[str, Any]]) -> List[str]:
    """Широкие темы вопросов для промпта вводной (без ответов и вариантов)."""
    topics = [(q.get("topic") or "").strip() for q in questions]
    topics = [topic for topic in topics if topic]
    return topics or ["общая эрудиция"]


def _previous_intros(state_data: Dict[str, Any]) -> List[str]:
    """Вводные прошлых выпусков (для проверки на шаблонность)."""
    return [
        p["intro"]
        for p in state_data.get("published", [])
        if isinstance(p, dict) and p.get("intro")
    ][-10:]


def _generate_texts_with_review(
    cfg, questions: List[Dict[str, Any]], previous_intros: List[str]
) -> Tuple[str, str, Dict[str, Any]]:
    """Сгенерировать вводную/заключительную и прогнать обязательное ревью.

    Одна первичная генерация плюс до `REVIEW_MAX_ATTEMPTS` регенераций при
    не-OK. При исчерпании попыток поднимает RuntimeError — публикация
    блокируется (код возврата != 0).

    Если вопросы берутся из `QUESTIONS_FILE`, а LLM-ключа нет (запуск без LLM),
    вводная/заключительная и ревью пропускаются — это сохраняет обратную
    совместимость режима без модели.
    """
    if getattr(cfg, "questions_file", None) and not (
        getattr(cfg, "llm_api_key", "") or ""
    ).strip():
        log.warning(
            "QUESTIONS_FILE задан без LLM_API_KEY: вводная, заключительная и "
            "ревью пропущены (режим запуска без LLM)."
        )
        return "", "", {"ok": True, "issues": []}

    if not cfg.intro_enabled and not cfg.conclusion_enabled:
        return "", "", {"ok": True, "issues": []}

    topics = _topics_of(questions)
    theme = (getattr(cfg, "topic", "") or "").strip()
    intro = ""
    outro = ""
    last_review: Dict[str, Any] = {"ok": True, "issues": []}
    for attempt in range(cfg.review_max_attempts + 1):
        if cfg.intro_enabled:
            intro = generate_intro(cfg, topics, previous_intros, theme=theme)
        if cfg.conclusion_enabled:
            outro = generate_conclusion(cfg, topics, len(questions), theme=theme)
        if not cfg.review_enabled:
            return intro, outro, {"ok": True, "issues": []}
        last_review = review.review_texts(
            cfg, intro, outro, questions, previous_intros, theme=theme
        )
        if last_review.get("ok"):
            log.info("Ревью вводной/заключительной пройдено")
            return intro, outro, last_review
        log.warning(
            "Ревью не пройдено (попытка %s/%s): %s",
            attempt + 1,
            cfg.review_max_attempts + 1,
            "; ".join(last_review.get("issues") or []),
        )
    raise RuntimeError(
        "Ревью вводной/заключительной не пройдено: "
        + "; ".join(last_review.get("issues") or [])
    )


def _choose_new_tests_theme(cfg, state: Dict[str, Any]) -> str | None:
    """Выбрать тему нового выпуска с ротацией (как в фото-потоке)."""
    themes = cfg.effective_new_tests_topics()
    used = list(state.get("used_themes") or [])
    candidates = [theme for theme in themes if theme not in used]
    if not candidates:
        log.warning("Все темы новых тестов использованы — сбрасываю список")
        state["used_themes"] = []
        candidates = themes
    if not candidates:
        return None
    return random.choice(candidates)


def _run_new_tests(cfg) -> int:
    """Отдельный поток «новых тестов»: свои состояние, темы и канал."""
    if not cfg.new_tests_enabled and not cfg.dry_run:
        log.error(
            "Поток новых тестов выключен. Включите его переменной "
            "NEW_TESTS_ENABLED=true (или задайте её в .env)."
        )
        return 1
    if not cfg.new_tests_enabled and cfg.dry_run:
        log.info(
            "Поток новых тестов выключен, но включён --dry-run: "
            "строю план без публикации."
        )

    state_data = load_state(cfg.new_tests_state_file)

    if not cfg.dry_run and not cfg.force:
        if state_data.get("last_publish_date") == today_utc():
            log.info(
                "За %s новый тест уже публиковали — пропускаю "
                "(используйте --force для повтора).",
                today_utc(),
            )
            return 0

    theme = _choose_new_tests_theme(cfg, state_data)
    if not theme:
        log.error("Не удалось выбрать тему нового теста")
        return 1

    # На время выпуска подменяем эрудиционные настройки на параметры потока.
    cfg.topic = theme
    cfg.count = cfg.new_tests_count
    cfg.survey_name = cfg.new_tests_survey_name
    # Новый поток всегда создаёт новую форму: --survey-id для него игнорируется.
    cfg.yandex_survey_id = None
    cfg.pass_scores = random.choice(cfg.effective_new_tests_pass_scores())
    cfg.segments = None
    log.info(
        "Новый тест: тема «%s», вопросов: %s, порог: %s",
        theme,
        cfg.count,
        cfg.pass_scores,
    )

    history = list(state_data.get("asked_questions") or [])
    if history:
        log.info("Учитываю ранее заданные вопросы: %s", len(history))
    checker = DuplicateChecker(history)

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
        try:
            questions = _collect_unique_questions(cfg, history, checker)
        except RuntimeError as exc:
            log.error("%s", exc)
            return 1

    minimum = cfg.effective_new_tests_min_questions()
    if len(questions) < minimum:
        log.error(
            "Вопросов получено %s — меньше минимума %s. Публикация отменена.",
            len(questions),
            minimum,
        )
        return 1

    log.info("Вопросов получено: %s", len(questions))
    for i, q in enumerate(questions, 1):
        correct = q["options"][q["correct_index"]]
        log.info("  %2d. %s  ->  %s", i, q["question"], correct)

    if cfg.dry_run:
        log.info(
            "DRY-RUN нового потока: обращения к Яндекс Формам и соцсетям не будет"
        )
        return 0

    previous_intros = _previous_intros(state_data)
    try:
        intro, outro, review_result = _generate_texts_with_review(
            cfg, questions, previous_intros
        )
    except Exception as exc:  # noqa: BLE001 - без трейсбека, публикация отменяется
        log.error("Не удалось подготовить вводную/заключительную: %s", exc)
        return 1

    # Публикация нового потока идёт в свой Telegram-канал (VK — опционально).
    cfg.tg_target_channel = cfg.new_tests_tg_target_channel
    new_tests_bot_token = getattr(cfg, "new_tests_tg_bot_token", "")
    if new_tests_bot_token:
        cfg.tg_bot_token = new_tests_bot_token
    cfg.publish_telegram = cfg.new_tests_publish_telegram
    cfg.publish_vk = cfg.new_tests_publish_vk

    client = YandexFormsClient(cfg.yandex_token, cfg.yandex_org_id, cfg.yandex_org_header)
    survey_id = publish_questions(cfg, questions, client, intro=intro, outro=outro)
    public_url = YandexFormsClient.public_url(survey_id)
    log.info("Готово. Публичная ссылка: %s", public_url)

    posted = publish_announcement(
        cfg, survey_id, len(questions), cfg.survey_name, intro=intro
    )
    if posted:
        log.info("Анонс опубликован: %s", ", ".join(posted))

    state_data["last_publish_date"] = today_utc()
    state_data.setdefault("published", []).append(
        {
            "date": today_utc(),
            "survey_id": survey_id,
            "url": public_url,
            "topic": theme,
            "intro": intro,
            "outro": outro,
            "review": review_result,
        }
    )
    asked = state_data.setdefault("asked_questions", [])
    asked.extend(q["question"] for q in questions)
    state_data["asked_questions"] = asked[-500:]
    used_themes = state_data.setdefault("used_themes", [])
    if theme not in used_themes:
        used_themes.append(theme)
    save_state(cfg.new_tests_state_file, state_data)
    log.info("Состояние сохранено: %s", cfg.new_tests_state_file)
    return 0


def main() -> int:
    args = parse_args()

    if (args.photo or args.new_tests) and (
        args.check or args.delete_survey or args.rename_survey
    ):
        log.error(
            "Флаги --check/--delete-survey/--rename-survey несовместимы "
            "с --photo/--new-tests"
        )
        return 2
    if args.photo and args.new_tests:
        log.error("Флаги --photo и --new-tests несовместимы")
        return 2

    if args.photo or args.new_tests:
        mode_label = "--photo" if args.photo else "--new-tests"
        candidates = [
            ("--count", args.count),
            ("--topic", args.topic),
            ("--survey-id", args.survey_id),
            ("--name", args.name),
            ("--photo-theme", args.photo_theme if args.new_tests else None),
        ]
        if args.photo:
            # В фото-потоке файл готовых вопросов не используется.
            candidates.append(("--questions-file", args.questions_file))
        ignored = [flag for flag, value in candidates if value is not None]
        if ignored:
            log.warning(
                "Эти параметры не применяются к режиму %s: %s",
                mode_label,
                ", ".join(ignored),
            )

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
    if args.photo:
        overrides["mode"] = "photo"
    if args.new_tests:
        overrides["mode"] = "new_tests"
    if args.photo_theme:
        overrides["photo_theme"] = args.photo_theme

    service_mode = bool(args.check or args.delete_survey or args.rename_survey)
    try:
        cfg = load_config(overrides, require_questions=not service_mode)
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    # Фото-поток: отдельная логика, эрудиция не затрагивается.
    if args.photo:
        return photo_flow.run(cfg, args)

    # Поток новых тестов: собственные состояние, темы и канал.
    if args.new_tests:
        return _run_new_tests(cfg)

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

    # 3) Вводная/заключительная и обязательное ревью
    previous_intros = _previous_intros(state_data)
    try:
        intro, outro, review_result = _generate_texts_with_review(
            cfg, questions, previous_intros
        )
    except Exception as exc:  # noqa: BLE001 - без трейсбека, публикация отменяется
        log.error("Не удалось подготовить вводную/заключительную: %s", exc)
        return 1

    # 4) Создаём и публикуем форму
    client = YandexFormsClient(cfg.yandex_token, cfg.yandex_org_id, cfg.yandex_org_header)
    survey_id = publish_questions(cfg, questions, client, intro=intro, outro=outro)
    public_url = YandexFormsClient.public_url(survey_id)
    log.info("Готово. Публичная ссылка: %s", public_url)

    # 5) Анонс в Telegram и VK (вводная добавляется в TG перед CTA)
    posted = publish_announcement(
        cfg, survey_id, len(questions), cfg.survey_name, intro=intro
    )
    if posted:
        log.info("Анонс опубликован: %s", ", ".join(posted))

    # 6) Сохраняем состояние (защита от дублей и от повторов вопросов)
    state_data["last_publish_date"] = today_utc()
    state_data.setdefault("published", []).append(
        {
            "date": today_utc(),
            "survey_id": survey_id,
            "url": public_url,
            "intro": intro,
            "outro": outro,
            "review": review_result,
        }
    )
    asked = state_data.setdefault("asked_questions", [])
    asked.extend(q["question"] for q in questions)
    state_data["asked_questions"] = asked[-500:]
    save_state(cfg.state_file, state_data)

    return 0


if __name__ == "__main__":
    sys.exit(main())