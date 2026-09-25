"""Фото-поток: выпуск теста с изображениями по одной теме.

Каждый выпуск посвящён ОДНОЙ теме (например «советские фильмы»), все вопросы
имеют одинаковую формулировку («Кто на фото?» / «Кадр из какого фильма?»).
Сущности темы и дистракторы генерирует LLM, изображения ищутся в фото-источниках
(`photo_sources`) и (опционально) проверяются vision-моделью (`vision`).

Состояние хранится отдельно от эрудиции — в `photo_state.json`.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any, Dict, List, Optional, Tuple

import photo_sources
import publisher
import vision
from config import Config
from llm import extract_json, post_chat
from state import load_state, save_state, today_utc
from yandex_forms import YandexFormsClient, build_quiz_settings

log = logging.getLogger(__name__)

# Сколько сущностей просить сверх нужного числа вопросов — запас на случай,
# когда для части сущностей не найдётся подходящего изображения.
EXTRA_ENTITIES = 5

# Число дистракторов на вопрос (плюс верный ответ = 4 варианта).
DISTRACTOR_COUNT = 3

# Глубина истории, которую храним в состоянии (как в main.py).
ASKED_HISTORY_LIMIT = 500

FILM_THEME_RE = re.compile(r"фильм|кино|сериал", re.IGNORECASE)
SOVIET_THEME_RE = re.compile(r"совет|ссср", re.IGNORECASE)
ACTOR_THEME_RE = re.compile(r"акт[её]р", re.IGNORECASE)

ENTITY_SYSTEM = (
    "Ты — составитель викторин с фотографиями. Ты подбираешь узнаваемые сущности "
    "и правдоподобные дистракторы, всегда отвечаешь строго в формате JSON."
)


def question_text(theme: str) -> str:
    """Формулировка вопроса в зависимости от темы выпуска."""
    if FILM_THEME_RE.search(theme):
        return "Кадр из какого фильма?"
    if ACTOR_THEME_RE.search(theme):
        return "Кто на фото?"
    return "Что (кто) изображено на фотографии?"


def is_film_theme(theme: str) -> bool:
    return bool(FILM_THEME_RE.search(theme))


def is_foreign_film_theme(theme: str) -> bool:
    """Иностранная кинотема: фильмы/кино, но не советские.

    Для таких тем кадры ищутся в англоязычных источниках (film-grab.com,
    movie-screencaps.com), поэтому названия фильмов нужны в оригинале.
    """
    return is_film_theme(theme) and not SOVIET_THEME_RE.search(theme or "")


def image_query(theme: str, entity: str) -> str:
    """Поисковый запрос для фото-источников.

    Для кино-тем используется голое название фильма: сначала маршрутизация
    выбирает профильный кино-источник, а префикс «film » только мешал бы
    поиску по названию.
    """
    return entity


def theme_sources(cfg, theme: str) -> List[str]:
    """Источники изображений, подходящие теме выпуска.

    Советские фильмы ищутся в ru.wikipedia (файловый namespace), иностранные —
    на film-grab.com / movie-screencaps.com, остальные темы — в открытых
    wikimedia/openverse. Для кино-тем wikimedia/openverse не примешиваются,
    иначе побеждают плакаты и афиши вместо кадров.
    """
    if SOVIET_THEME_RE.search(theme or ""):
        return ["ruwiki_film"] if cfg.film_ru_enabled else []
    if is_film_theme(theme):
        sources: List[str] = []
        if cfg.film_grab_enabled:
            sources.append("filmgrab")
        if cfg.movie_screencaps_enabled:
            sources.append("movscreencaps")
        return sources
    return ["wikimedia", "openverse"]


def available_theme_sources(cfg, theme: str) -> List[str]:
    """Источники темы, реально включённые в конфиге.

    `theme_sources` даёт статический список под тему, но фактически активные
    источники определяет `cfg.enabled_photo_sources()` (PHOTO_SOURCES и
    кино-флаги). Пересечение не даёт выбрать тему, все источники которой
    выключены: иначе поиск вернёт 0 кандидатов и выпуск провалится.
    """
    enabled = set(cfg.enabled_photo_sources())
    return [source for source in theme_sources(cfg, theme) if source in enabled]


def _theme_skip_reason(cfg, theme: str) -> Optional[str]:
    """Причина, по которой тему нельзя использовать (или None)."""
    if available_theme_sources(cfg, theme):
        return None
    if SOVIET_THEME_RE.search(theme or ""):
        return "не включён FILM_RU_ENABLED"
    if is_film_theme(theme):
        return "не включены FILM_GRAB_ENABLED/MOVIE_SCREENCAPS_ENABLED"
    return "нет доступных источников"


def _capitalize(theme: str) -> str:
    return theme[:1].upper() + theme[1:] if theme else theme


def _entity_hint(theme: str) -> str:
    if is_foreign_film_theme(theme):
        return (
            "известных иностранных фильмов; указывай ОРИГИНАЛЬНЫЕ названия "
            "(как правило, на английском), по которым можно найти кадры"
        )
    if is_film_theme(theme):
        return "известных фильмов (узнаваемых по кадру)"
    if ACTOR_THEME_RE.search(theme):
        return "известных актёров"
    return "конкретных объектов, персон или мест, которые можно узнать на фото"


def _name_language_rule(theme: str) -> str:
    """Требование к языку названий в промпте генерации сущностей.

    Иностранные фильмы ищутся по оригинальным (обычно английским) названиям,
    советские — по русским.
    """
    if is_foreign_film_theme(theme):
        return (
            "Названия указывай ОРИГИНАЛЬНЫЕ (как правило, на английском — "
            "на языке оригинала фильма), чтобы по ним находились кадры "
            "в англоязычных источниках."
        )
    if SOVIET_THEME_RE.search(theme or ""):
        return "Названия указывай на русском языке."
    return "Пункты указывай на русском языке."


def _chat_json(cfg, system: str, user: str) -> Dict[str, Any]:
    """Вызвать Chat Completions (с ретраями) и вернуть распарсенный JSON."""
    payload = {
        "model": cfg.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": cfg.llm_temperature,
        "response_format": {"type": "json_object"},
    }
    data = post_chat(cfg, payload)
    content = data["choices"][0]["message"]["content"]
    return extract_json(content)


def _generate_entities(
    cfg, theme: str, count: int, used: List[str]
) -> List[str]:
    """Сгенерировать сущности темы, исключая уже использованные."""
    avoid_block = ""
    if used:
        listed = "\n".join(f"- {item}" for item in used[-100:])
        avoid_block = (
            "\nНе повторяй сущности, которые уже были раньше:\n"
            f"{listed}\n"
        )
    user = (
        f"Составь список ровно из {count} {_entity_hint(theme)} по теме «{theme}».\n"
        "Требования:\n"
        f"- {_name_language_rule(theme)}\n"
        "- все пункты — из этой темы, без повторов;\n"
        "- только широко известные, однозначно узнаваемые варианты;\n"
        "- разные пункты не должны быть похожи друг на друга.\n"
        f"{avoid_block}\n"
        "Верни JSON строго такого вида:\n"
        '{"entities": ["...", "..."]}\n'
        "Никакого текста кроме JSON."
    )
    data = _chat_json(cfg, ENTITY_SYSTEM, user)
    items = data.get("entities") or data.get("items") or []
    used_canon = {str(x).strip().lower() for x in used}
    result: List[str] = []
    for item in items:
        name = str(item).strip()
        if name and name.lower() not in used_canon:
            used_canon.add(name.lower())
            result.append(name)
    return result


def _generate_distractors(cfg, theme: str, entity: str) -> List[str]:
    """Сгенерировать дистракторы строго того же класса, что и верный ответ."""
    language_rule = ""
    if is_foreign_film_theme(theme):
        language_rule = (
            "Дистракторы — ОРИГИНАЛЬНЫЕ названия иностранных фильмов "
            "(как правило, на английском), того же языка, что и правильный ответ.\n"
        )
    elif SOVIET_THEME_RE.search(theme or ""):
        language_rule = "Дистракторы — названия советских фильмов на русском языке.\n"
    user = (
        f"Тема викторины — «{theme}». Правильный ответ: «{entity}».\n"
        f"Придумай ровно {DISTRACTOR_COUNT} дистрактора — правдоподобные, но "
        "НЕправильные варианты СТРОГО того же класса, что и правильный ответ "
        "(та же категория и уровень известности, без повторов).\n"
        f"{language_rule}"
        "Верни JSON строго такого вида:\n"
        '{"distractors": ["...", "...", "..."]}\n'
        "Никакого текста кроме JSON."
    )
    data = _chat_json(cfg, ENTITY_SYSTEM, user)
    items = data.get("distractors") or data.get("items") or []
    return [str(item).strip() for item in items if str(item).strip()]


def _build_options(cfg, theme: str, entity: str) -> Optional[Tuple[List[str], int]]:
    """Собрать 4 варианта (верный + дистракторы) и перемешать.

    Возвращает (options, correct_index) или None, если дистракторов не хватило
    или LLM недоступна.
    """
    try:
        distractors = _generate_distractors(cfg, theme, entity)
    except Exception as exc:  # noqa: BLE001 - сбой LLM не должен ронять запуск
        log.warning("Не удалось получить дистракторы для «%s»: %s", entity, exc)
        return None

    seen = {entity.strip().lower()}
    unique: List[str] = []
    for item in distractors:
        name = item.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            unique.append(name)
    if len(unique) < DISTRACTOR_COUNT:
        log.warning("Мало дистракторов для «%s»: %s", entity, len(unique))
        return None

    options = [entity] + unique[:DISTRACTOR_COUNT]
    random.shuffle(options)
    return options, options.index(entity)


def _search_candidates(cfg, theme: str, entity: str) -> List[photo_sources.ImageCandidate]:
    query = image_query(theme, entity)
    sources = available_theme_sources(cfg, theme)
    if not sources:
        log.info("Для темы «%s» нет доступных источников — поиск пропущен", theme)
        return []
    try:
        return photo_sources.search_images(
            cfg,
            query,
            limit=max(3, cfg.photo_max_image_attempts),
            sources=sources,
        )
    except Exception as exc:  # noqa: BLE001 - источник ненадёжен
        log.warning("Поиск изображений для «%s» не удался: %s", entity, exc)
        return []


def _pick_image(
    cfg, theme: str, entity: str
) -> Optional[Tuple[bytes, str, photo_sources.ImageCandidate]]:
    """Найти и скачать первое подходящее изображение для сущности."""
    attempts = 0
    for candidate in _search_candidates(cfg, theme, entity):
        if attempts >= cfg.photo_max_image_attempts:
            break
        attempts += 1
        try:
            data, mime = photo_sources.download_image(cfg, candidate)
        except RuntimeError as exc:
            log.info("Изображение не скачано для «%s»: %s", entity, exc)
            continue
        ok, reason = vision.verify_image(cfg, data, mime, entity, theme)
        if not ok:
            log.info("Vision отклонила изображение для «%s»: %s", entity, reason)
            continue
        return data, mime, candidate
    return None


def _image_meta(candidate: photo_sources.ImageCandidate) -> Dict[str, Any]:
    return {
        "source": candidate.source,
        "url": candidate.url,
        "page_url": candidate.page_url,
        "title": candidate.title,
        "author": candidate.author,
        "license": candidate.license,
    }


def _filename(index: int, mime: str) -> str:
    mime = (mime or "").lower()
    if "png" in mime:
        ext = "png"
    elif "webp" in mime:
        ext = "webp"
    else:
        ext = "jpg"
    return f"photo_{index}.{ext}"


def _warn_wikimedia(cfg) -> None:
    """Предупредить, если активен wikimedia без описательного User-Agent."""
    if "wikimedia" in cfg.enabled_photo_sources() and not cfg.wikimedia_user_agent:
        log.warning(
            "Источник wikimedia активен, но WIKIMEDIA_USER_AGENT не задан. "
            "Wikimedia требует описательный User-Agent — запросы могут отклоняться."
        )


def _choose_theme(cfg, state: Dict[str, Any]) -> Optional[str]:
    """Выбрать тему выпуска: заданную вручную или случайную неиспользованную.

    Темы, для которых `theme_sources` пуст (например, фильмы при выключенных
    кино-флагах), пропускаются — иначе боевой запуск соберёт неправильные фото.
    """
    if cfg.photo_theme:
        reason = _theme_skip_reason(cfg, cfg.photo_theme)
        if reason:
            log.error("Тема «%s» недоступна: %s", cfg.photo_theme, reason)
            return None
        log.info("Тема задана явно: %s", cfg.photo_theme)
        return cfg.photo_theme

    themes = cfg.effective_photo_themes()
    available: List[str] = []
    for theme in themes:
        reason = _theme_skip_reason(cfg, theme)
        if reason:
            log.info("Тема «%s» пропущена: %s", theme, reason)
            continue
        available.append(theme)

    used = list(state.get("used_themes") or [])
    candidates = [t for t in available if t not in used]
    if not candidates:
        log.warning("Все доступные темы уже использованы — сбрасываю список тем")
        state["used_themes"] = []
        candidates = available
    if not candidates:
        return None
    return random.choice(candidates)


def _dry_run(
    cfg, theme: str, question: str, entities: List[str], need: int
) -> int:
    log.info(
        "DRY-RUN фото-теста: Яндекс Формы не вызываются, изображения не "
        "скачиваются, vision пропущен."
    )
    planned = 0
    for entity in entities:
        if planned >= need:
            break
        built = _build_options(cfg, theme, entity)
        if not built:
            log.info("Пропускаю сущность «%s»: не удалось получить варианты", entity)
            continue
        options, correct_index = built
        candidates = _search_candidates(cfg, theme, entity)
        if not candidates:
            log.info("Пропускаю сущность «%s»: изображение не найдено", entity)
            continue
        planned += 1
        log.info("  %2d. %s", planned, question)
        log.info("      правильный ответ: %s", entity)
        for i, option in enumerate(options):
            log.info("        %s%s", option, " *" if i == correct_index else "")
        log.info("      кандидаты изображения:")
        for candidate in candidates[: cfg.photo_max_image_attempts]:
            log.info("        [%s] %s", candidate.source, candidate.url)

    minimum = cfg.effective_photo_min_questions()
    if planned < minimum:
        log.error("План содержит %s вопросов — меньше минимума %s", planned, minimum)
        return 1
    log.info("DRY-RUN: запланировано вопросов: %s", planned)
    return 0


def _build_questions(
    cfg, theme: str, question: str, entities: List[str], need: int
) -> List[Dict[str, Any]]:
    """Подготовить вопросы с изображениями (до набора `need` штук)."""
    questions: List[Dict[str, Any]] = []
    for entity in entities:
        if len(questions) >= need:
            break
        built = _build_options(cfg, theme, entity)
        if not built:
            log.info("Пропускаю сущность «%s»: не удалось получить варианты", entity)
            continue
        options, correct_index = built
        picked = _pick_image(cfg, theme, entity)
        if not picked:
            log.info("Пропускаю сущность «%s»: изображение не подобрано", entity)
            continue
        data, mime, candidate = picked
        questions.append(
            {
                "topic": theme,
                "question": question,
                "options": options,
                "correct_index": correct_index,
                "theme": theme,
                "entity": entity,
                "image": _image_meta(candidate),
                "_image_bytes": data,
                "_image_mime": mime,
            }
        )
        log.info(
            "  + вопрос %s: «%s» [%s, %s]",
            len(questions),
            entity,
            candidate.source,
            candidate.license or "лицензия не указана",
        )
    return questions


def _publish(
    cfg,
    state: Dict[str, Any],
    theme: str,
    question: str,
    entities: List[str],
    need: int,
) -> int:
    questions = _build_questions(cfg, theme, question, entities, need)
    minimum = cfg.effective_photo_min_questions()
    if len(questions) < minimum:
        log.error(
            "Подготовлено %s вопросов — меньше минимума %s. Публикация отменена.",
            len(questions),
            minimum,
        )
        return 1

    client = YandexFormsClient(cfg.yandex_token, cfg.yandex_org_id, cfg.yandex_org_header)

    name = f"{cfg.photo_survey_name.strip().rstrip('?')}? {_capitalize(theme)}"
    if cfg.number_surveys:
        name = client.next_survey_name(name)
    survey_id = client.create_survey(name)
    log.info("Создана форма «%s»: %s", name, survey_id)

    added: List[Dict[str, Any]] = []
    for i, q in enumerate(questions, 1):
        try:
            uploaded = client.upload_image(
                survey_id,
                q["_image_bytes"],
                _filename(i, q["_image_mime"]),
                q["_image_mime"],
            )
            client.add_enum_question(
                survey_id,
                question=q["question"],
                options=q["options"],
                correct_index=q["correct_index"],
                shuffle=cfg.shuffle,
                image=uploaded,
            )
        except Exception as exc:  # noqa: BLE001 - один вопрос не должен ронять выпуск
            log.warning("Не удалось добавить вопрос «%s»: %s", q["entity"], exc)
            continue
        q.pop("_image_bytes", None)
        q.pop("_image_mime", None)
        added.append(q)

    if len(added) < minimum:
        log.error(
            "В форму добавлено %s вопросов — меньше минимума %s. "
            "Публикация отменена, удаляю черновик формы %s.",
            len(added),
            minimum,
            survey_id,
        )
        try:
            client.delete_survey(survey_id)
            log.info("Черновик формы %s удалён", survey_id)
        except Exception as exc:  # noqa: BLE001 - не мешаем корректному коду возврата
            log.warning("Не удалось удалить черновик формы %s: %s", survey_id, exc)
        return 1

    quiz = build_quiz_settings(cfg, total=len(added))
    client.update_survey(survey_id, {"quiz": quiz, "stats": cfg.stats, "need_auth": False})
    log.info("Настройки теста применены (stats=%s)", cfg.stats)

    client.set_access(survey_id, access="public", action="submit")
    log.info("Доступ к форме: публичный (заполнение)")

    if cfg.publish:
        client.publish(survey_id)
        log.info("Форма опубликована")

    public_url = YandexFormsClient.public_url(survey_id)
    log.info("Готово. Публичная ссылка: %s", public_url)

    posted = publisher.publish_announcement(cfg, survey_id, len(added), name)
    if posted:
        log.info("Анонс опубликован: %s", ", ".join(posted))

    # Состояние фото-потока: дата, выпуск, тема, использованные сущности.
    state["last_publish_date"] = today_utc()
    state.setdefault("published", []).append(
        {
            "date": today_utc(),
            "survey_id": survey_id,
            "url": public_url,
            "theme": theme,
            # автор/лицензия — только в состоянии, в форму и анонс не выводятся
            "images": [q["image"] for q in added],
        }
    )
    asked = state.setdefault("asked_questions", [])
    asked.extend(f"{q['theme']}: {q['entity']}" for q in added)
    state["asked_questions"] = asked[-ASKED_HISTORY_LIMIT:]

    used_themes = state.setdefault("used_themes", [])
    if theme not in used_themes:
        used_themes.append(theme)

    used_entities = state.setdefault("used_entities", {})
    used_entities.setdefault(theme, []).extend(q["entity"] for q in added)

    save_state(cfg.photo_state_file, state)
    log.info("Состояние сохранено: %s", cfg.photo_state_file)
    return 0


def run(cfg: Config, args: Any = None) -> int:
    """Выполнить один запуск фото-потока. Возвращает код возврата."""
    if not cfg.photo_flow_enabled:
        log.error(
            "Фото-поток выключен. Включите его переменной окружения "
            "PHOTO_FLOW_ENABLED=true (или задайте её в .env)."
        )
        return 1

    _warn_wikimedia(cfg)

    state = load_state(cfg.photo_state_file)

    if not cfg.dry_run and not cfg.force:
        if state.get("last_publish_date") == today_utc():
            log.info(
                "За %s фото-тест уже публиковали — пропускаю "
                "(используйте --force для повтора).",
                today_utc(),
            )
            return 0

    theme = _choose_theme(cfg, state)
    if not theme:
        log.error(
            "Не удалось выбрать тему фото-теста: нет доступных тем. "
            "Проверьте PHOTO_THEMES и флаги кино-источников "
            "(FILM_RU_ENABLED, FILM_GRAB_ENABLED, MOVIE_SCREENCAPS_ENABLED)."
        )
        return 1

    question = question_text(theme)
    need = cfg.photo_questions_count
    log.info(
        "Фото-тест: тема «%s», вопрос «%s», вопросов: %s",
        theme,
        question,
        need,
    )

    used_entities = list((state.get("used_entities") or {}).get(theme, []))
    try:
        entities = _generate_entities(cfg, theme, need + EXTRA_ENTITIES, used_entities)
    except Exception as exc:  # noqa: BLE001 - понятная ошибка вместо трейсбека
        log.error("Не удалось получить сущности темы «%s»: %s", theme, exc)
        return 1

    if not entities:
        log.error("Модель не вернула сущностей для темы «%s»", theme)
        return 1
    log.info("Сущностей для темы: %s", len(entities))
    for entity in entities:
        log.info("  - %s", entity)

    if cfg.dry_run:
        return _dry_run(cfg, theme, question, entities, need)

    return _publish(cfg, state, theme, question, entities, need)