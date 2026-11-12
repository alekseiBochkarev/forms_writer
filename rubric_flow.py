"""Эксперимент с рубриками: выпуск теста с фиксированным форматом серии.

Рубрика — это серия выпусков с одним форматом (своя тема-источник, число
вопросов, порог, шаблоны заголовков и вопрос для комментариев). Заголовок
собирается из шаблонов рубрики с номером серии, порогом и числом вопросов, а не
из общей фото-библиотеки. Состояние рубрик хранится отдельно — в
`rubrics_state.json`.

Источник вопросов задаётся рубрикой:
  - `photo` — переиспользует фото-конвейер `photo_flow` (тема, сущности,
    изображения, vision), но со своим заголовком, порогом и комментарием;
  - `trap` — вопросы-ловушки на эрудицию (`llm.generate_trap_questions`) без
    картинок: обычный enum-конвейер с фактчекингом и дедупликацией;
  - `mixed` — смешанный выпуск: часть вопросов с фото из фото-конвейера,
    часть — текстовые вопросы по той же теме (`llm.generate_topic_questions`).
    Вопросы чередуются, чтобы кадры работали визуальными крючками по всей форме.
"""

from __future__ import annotations

import logging
import math
import os
import re
from typing import Any, Dict, List, Optional

import photo_flow
import publisher
from config import Config, KNOWN_RUBRIC_SOURCES, Rubric
from dedup import DuplicateChecker
from llm import generate_topic_questions, generate_trap_questions
from state import (
    allow_publish_rubric,
    load_state,
    next_series_no,
    record_publish,
    rubric_entries,
    save_state,
    today_utc,
)

log = logging.getLogger(__name__)

# Статичная подстановка {wrong_pct} для рубрик-ловушек: используется, пока в
# истории рубрики нет метрики доли ошибшихся (её пишет rubric_metrics.py).
STATIC_WRONG_PCT = "меньше половины"

# Минимум вопросов-ловушек для выпуска: меньше — отмена публикации (как в
# фото-потоке, где недобор отменяет выпуск).
TRAP_MIN_QUESTIONS = 6

# Смешанная рубрика: сколько текстовых кандидатов просить сверх нужного —
# запас на случай, когда фактчек и дедупликация отбросят часть вопросов.
MIXED_EXTRA_TEXT = 4

# Минимальная доля собранных вопросов от плана выпуска. Недобор (например,
# не нашлось кадров или фактчек отбросил слишком много фактов) отменяет выпуск.
MIXED_MIN_RATIO = 0.8


def _mixed_extra_text(need: int) -> int:
    """Запас текстовых кандидатов сверх плана: не меньше трети нужного числа.

    Для крупного выпуска (15 текстовых вопросов из 30) базовых четырёх мало:
    недобор добирается кадрами и ломает баланс «15 фото + 15 текстовых».
    """
    return max(MIXED_EXTRA_TEXT, math.ceil(max(0, need) / 3))


def latest_wrong_pct(state: Dict[str, Any], rubric_id: str) -> Optional[str]:
    """Вернуть текстовую метрику «ошиблись NN%» по истории публикаций рубрики.

    Читает поле `wrong_pct` из последних записей `published[]` данной рубрики
    (его пишет `rubric_metrics.py` на этапе 4). Берётся последнее целое значение в
    диапазоне 0..100 и возвращается числом без знака процента — шаблоны заголовков
    сами дописывают `%` (плейсхолдер `{wrong_pct}%`). Если метрики ещё нет —
    None (вызывающий код подставит мягкую статичную формулировку).
    """
    published = state.get("published")
    if not isinstance(published, list):
        return None
    for entry in reversed(published):
        if not isinstance(entry, dict) or entry.get("rubric_id") != rubric_id:
            continue
        raw = entry.get("wrong_pct")
        try:
            pct = int(round(float(raw)))
        except (TypeError, ValueError):
            continue
        if 0 <= pct <= 100:
            return str(pct)
    return None


def build_rubric_title(
    rubric: Rubric,
    series_no: int,
    count: int,
    used_titles: Optional[List[str]] = None,
    correct_answers: Optional[List[str]] = None,
    wrong_pct: Optional[str] = None,
    theme: Optional[str] = None,
) -> str:
    """Собрать заголовок рубрики из её шаблонов с анти-спойлером и историей.

    Плейсхолдеры: `{series_no}` (номер выпуска вида «№7»), `{threshold}`,
    `{count}`/`{n}` (число вопросов), `{photo_count}` (для смешанных рубрик —
    сколько вопросов идёт с фото), `{wrong_pct}` (для рубрик-ловушек —
    динамическая метрика «NN%» из истории либо мягкая статичная формулировка).
    Анти-спойлер, проверка соответствия теме и защита от повторов — общая логика
    с фото-потоком (`photo_flow.render_title`).
    """
    fields = {
        "series_no": f"№{series_no}",
        "threshold": rubric.threshold,
        "count": count,
        "n": count,
        "photo_count": rubric.photo_count,
        "wrong_pct": (wrong_pct or "").strip() or STATIC_WRONG_PCT,
    }
    title = photo_flow.render_title(
        list(rubric.title_templates),
        fields,
        used_titles=used_titles,
        correct_answers=correct_answers,
        fallback=rubric.series_name,
        theme=theme or "",
    )
    return title or rubric.series_name


def _rubric_theme(
    cfg, rubric: Rubric, state: Dict[str, Any]
) -> Optional[str]:
    """Выбрать тему выпуска рубрики.

    Темы берёт из `rubric.themes` (если заданы) — иначе заголовок серии может
    не соответствовать содержанию (например «угадаете картинки», а в выпуске
    животные). Ротация и проверка доступности источников — общие с фото-потоком.
    """
    override = rubric.theme_candidates(cfg.effective_photo_themes())
    if rubric.themes and not override:
        log.error(
            "Рубрика «%s»: ни одна из тем %s не найдена в PHOTO_THEMES",
            rubric.id,
            rubric.themes,
        )
        return None
    return photo_flow._choose_theme(cfg, state, themes_override=override)


def _published_questions(cfg, survey_id: str) -> List[Dict[str, Any]]:
    """Загрузить вопросы опубликованной формы в формате enum-конвейера.

    Реаккует вопросы формы (та же пара ручек, что и `--check`/`rubric_metrics`:
    `get_questions`) в схемы `{question, options, correct_index}`, пригодные для
    `_publish_plain_form`. Метку темы (`topic`) ставит прочно: при дедупе
    ловушек она не участвует, а для отображения достаточно пустой. Возвращает
    [] при сбое или если в форме нет пригодных вопросов.
    """
    # Импорт здесь — боевая зависимость (клиент Яндекс Форм) не тянется в тестах
    # выбора кандидата и в dry-run.
    from yandex_forms import YandexFormsClient

    client = YandexFormsClient(
        cfg.yandex_token, cfg.yandex_org_id, cfg.yandex_org_header
    )
    result: List[Dict[str, Any]] = []
    for q in client.get_questions(survey_id):
        label = (q.get("label") or "").strip()
        # Варианты enum приходят как [{label, correct}, ...] (см. hard_questions).
        options = [str(item.get("label", "")) for item in (q.get("options") or [])]
        correct_index = next(
            (i for i, item in enumerate(q.get("options") or []) if item.get("correct")),
            None,
        )
        if not label or len(options) < 2 or correct_index is None:
            continue
        result.append(
            {
                "topic": "",
                "question": label,
                "options": options,
                "correct_index": correct_index,
            }
        )
    return result


def pick_repack_candidate(
    state: Dict[str, Any],
    rubric_id: str,
    *,
    wrong_pct_threshold: int,
    min_answers: int,
) -> Optional[Dict[str, Any]]:
    """Выбрать провальную публикацию рубрики для репака (или None).

    Кандидат — последняя (самая свежая) запись `published[]` данной рубрики, у
    которой уже есть метрики и выполняются условия:
      - `wrong_pct >= wrong_pct_threshold` (доля ошибшихся выше порога провала);
      - `answers_count >= min_answers` (выборка достаточна);
      - запись ещё не репаковалась — ни одна более поздняя публикация этой
        рубрики не ссылается на неё через `repack_of`.
    Иначе — None (репак не нужен).
    """
    published = state.get("published")
    if not isinstance(published, list):
        return None
    entries = rubric_entries(state, rubric_id)
    if not entries:
        return None

    # id уже переупакованных публикаций: на них ссылается чужой repack_of.
    repacked_ids = set()
    for entry in entries:
        origin = entry.get("repack_of")
        if origin:
            repacked_ids.add(str(origin))

    for entry in reversed(entries):
        # репак-потомок сам не является кандидатом (перепаковывать репак не нужно)
        if entry.get("repack_of"):
            continue
        survey_id = str(entry.get("survey_id") or "")
        if not survey_id or survey_id in repacked_ids:
            continue
        wrong_pct = _as_number(entry.get("wrong_pct"))
        answers_count = _as_number(entry.get("answers_count"))
        if wrong_pct is None or answers_count is None:
            continue
        if wrong_pct >= wrong_pct_threshold and answers_count >= min_answers:
            return entry
    return None


def _already_repacked_today(
    state: Dict[str, Any], rubric_id: str, survey_id: str
) -> bool:
    """Нужно ли пропустить репак: лимиты «не чаще раза в день» для репака.

    Для репака НЕ применяется общее ограничение «раз в UTC-день на рубрику» —
    репак публикуется в том же дне, что и выпуск серии. Вместо него:
      - не более одного репака в UTC-день (сегодня уже есть запись с `repack_of`);
      - один и тот же исходник не репакуется дважды (есть потомок с
        `repack_of == survey_id`).
    Возвращает True, если репак делать нельзя (вызывающий выходит с кодом 0).
    """
    today = today_utc()
    for entry in rubric_entries(state, rubric_id):
        origin = entry.get("repack_of")
        if not origin:
            continue
        ts = str(entry.get("publish_ts") or entry.get("date") or "")
        if ts[:10] == today:
            log.info(
                "За %s уже выполнен репак рубрики «%s» — повторный репак сегодня "
                "пропущен.",
                today,
                rubric_id,
            )
            return True
        if str(origin) == survey_id:
            log.info(
                "Форма %s рубрики «%s» уже переупакована ранее — репак пропущен.",
                survey_id,
                rubric_id,
            )
            return True
    return False


def _as_number(raw: Any) -> Optional[float]:
    """Привести значение метрики к float (None — если это не число)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _repack_title(
    rubric: Rubric,
    series_no: int,
    count: int,
    used_titles: List[str],
    wrong_pct: Optional[str],
) -> str:
    """Заголовок репака: неиспользованный шаблон серии (анти-спойлер не нужен).

    В репаке правильные ответы не передаются в `render_title`, поэтому фильтр
    спойлеров не срабатывает и доступен любой шаблон. `used_titles` — заголовки
    самой переупаковываемой серии: среди них выбирается свежий; если все
    израсходованы — `render_title` вернёт один из них (повтор допустим).
    """
    return build_rubric_title(
        rubric,
        series_no,
        count,
        used_titles=used_titles,
        correct_answers=None,
        wrong_pct=wrong_pct,
    )


def run_rubric_repack(
    cfg: Config,
    state: Dict[str, Any],
    rubric_id: str,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """Переупаковать провальную публикацию рубрики-ловушки. Код возврата.

    Берёт из состояния последнюю провальную публикацию рубрики (много ошибшихся
    при достаточной выборке, ещё не репакованную) и публикует НОВУЮ форму с теми
    же вопросами, но другим заголовком-шаблоном серии; в состояние запись
    помечается `repack_of` исходной формы. Дедуп и генерация новых вопросов
    пропускаются — содержание остаётся прежним. Если кандидатов нет — выход 0.
    """
    if cfg.rubrics is None:
        log.error("Реестр рубрик не загружен")
        return 1
    rubric = cfg.rubrics.get(rubric_id)
    if rubric is None:
        log.error("Неизвестная рубрика «%s»", rubric_id)
        return 1
    if rubric.source != "trap":
        # Репакаем только ловушки: у фото-рубрик вопросы завязаны на картинки,
        # пересобрать форму из state по ним нельзя.
        log.info("Рубрика «%s» не trap — репак не применяется.", rubric_id)
        return 0

    candidate = pick_repack_candidate(
        state,
        rubric_id,
        wrong_pct_threshold=cfg.rubric_repack_wrong_pct,
        min_answers=cfg.rubric_repack_min_answers,
    )
    if candidate is None:
        log.info("Репака рубрики «%s» не нужен: кандидатов нет.", rubric_id)
        return 0

    origin_id = str(candidate.get("survey_id") or "")
    origin_title = str(candidate.get("title") or "")
    log.info(
        "Кандидат на репак рубрики «%s»: форма %s (ошиблись %s%%, ответов %s), "
        "заголовок «%s».",
        rubric_id,
        origin_id,
        candidate.get("wrong_pct"),
        candidate.get("answers_count"),
        origin_title,
    )

    # Заголовки этой же серии (переупаковываемой) — чтобы репак взял свежий
    # шаблон; исходный заголовок добавляем явно, даже если он выпал из окна.
    title_history = getattr(cfg, "photo_title_history", 10) or 10
    series_title_history = [
        str(entry.get("title"))
        for entry in rubric_entries(state, rubric_id)
        if entry.get("title")
    ][-title_history:]
    if origin_title and origin_title not in series_title_history:
        series_title_history.append(origin_title)

    series_no = int(candidate.get("series_no") or 1)
    count = int(candidate.get("count") or rubric.questions_count)
    wrong_pct = latest_wrong_pct(state, rubric_id)
    title = _repack_title(rubric, series_no, count, series_title_history, wrong_pct)
    log.info("Новый заголовок репака: %s", title)

    if dry_run:
        log.info(
            "DRY-RUN репака: Яндекс Формы не вызываются, форма с вопросами "
            "формы %s не создаётся, состояние не пишется.",
            origin_id,
        )
        return 0

    if not cfg.yandex_token or not cfg.yandex_org_id:
        log.error("Для репака не заданы YANDEX_FORMS_TOKEN / YANDEX_ORG_ID")
        return 1
    # Ограничение «раз в UTC-день на рубрику» к репаку не применяется: он
    # публикуется в том же дне, что и выпуск серии. Действуют свои лимиты —
    # один репак в день и один репак на исходник.
    if _already_repacked_today(state, rubric_id, origin_id):
        return 0

    try:
        questions = _published_questions(cfg, origin_id)
    except Exception as exc:  # noqa: BLE001 - понятная ошибка вместо трейсбека
        log.error("Не удалось загрузить вопросы формы %s для репака: %s", origin_id, exc)
        return 1
    if len(questions) < TRAP_MIN_QUESTIONS:
        log.error(
            "В форме %s удалось восстановить %s вопросов — меньше минимума %s. "
            "Репак отменён.",
            origin_id,
            len(questions),
            TRAP_MIN_QUESTIONS,
        )
        return 1

    def title_builder(_used, _answers):
        return title

    result = _publish_plain_form(
        cfg,
        questions,
        minimum=TRAP_MIN_QUESTIONS,
        title_builder=title_builder,
        comment_prompt=rubric.comment_prompt,
    )
    if result is None:
        return 1

    base_title = result["base_title"]
    added = result["added"]
    survey_id = result["survey_id"]
    public_url = result["public_url"]

    # Записываем состояние СРАЗУ после публикации формы — до анонса: сбой
    # анонса не должен приводить к повторной публикации той же формы при
    # следующем запуске. Метрику доли ошибшихся (`wrong_pct`) сюда НЕ переносим:
    # её пишет только `rubric_metrics` по реальным ответам репака, иначе
    # `latest_wrong_pct` и отчёт удваивали бы вклад одного провала.
    entry = record_publish(
        state,
        survey_id=survey_id,
        url=public_url,
        rubric_id=rubric_id,
        series_no=series_no,
        threshold=rubric.threshold,
        count=len(added),
        repack_of=origin_id,
    )
    entry["title"] = base_title
    entry["repack"] = True

    used_titles_state = state.setdefault("used_titles", [])
    used_titles_state.append(base_title)
    state["used_titles"] = used_titles_state[-photo_flow.TITLE_HISTORY_LIMIT:]

    save_state(cfg.rubrics_state_file, state)
    log.info("Репак опубликован: %s (исходная форма %s).", public_url, origin_id)
    log.info("Состояние сохранено: %s", cfg.rubrics_state_file)

    try:
        posted = publisher.publish_announcement(cfg, survey_id, len(added), base_title)
    except Exception as exc:  # noqa: BLE001 - анонс не должен ронять выпуск
        log.warning("Не удалось опубликовать анонс репака: %s", exc)
    else:
        if posted:
            log.info("Анонс опубликован: %s", ", ".join(posted))
    return 0


def _dry_run(
    cfg, state: Dict[str, Any], rubric: Rubric, theme: str, series_no: int
) -> int:
    """Сухая сборка выпуска рубрики без обращений к API (паттерн photo_flow)."""
    log.info(
        "DRY-RUN рубрики «%s»: Яндекс Формы не вызываются, изображения не "
        "скачиваются, vision пропущен, LLM не вызывается.",
        rubric.id,
    )
    question = photo_flow.question_text(theme)
    need = rubric.questions_count
    log.info(
        "Рубрика «%s» (%s): тема «%s», вопрос «%s», вопросов: %s, порог: %s/%s",
        rubric.id,
        rubric.series_name,
        theme,
        question,
        need,
        rubric.threshold,
        need,
    )

    # Сухие сущности: синтетические, чтобы показать план без LLM и сети.
    correct_answers = [f"сущность {i + 1}" for i in range(need)]
    title_history = getattr(cfg, "photo_title_history", 10) or 10
    used_titles = list(state.get("used_titles") or [])[-title_history:]
    title = build_rubric_title(
        rubric,
        series_no,
        need,
        used_titles=used_titles,
        correct_answers=correct_answers,
        theme=theme,
    )
    log.info("  заголовок: %s", title)

    for i in range(need):
        answer = correct_answers[i]
        log.info("  %2d. %s", i + 1, question)
        log.info("      правильный ответ: %s", answer)
        for option in (answer, "дистрактор 1", "дистрактор 2", "дистрактор 3"):
            log.info("        %s%s", option, " *" if option == answer else "")

    if (rubric.comment_prompt or "").strip():
        log.info("  + вопрос для комментариев: %s", rubric.comment_prompt.strip())

    log.info(
        "DRY-RUN: будет опубликовано вопросов %s (порог %s), состояние — %s",
        need,
        rubric.threshold,
        cfg.rubrics_state_file,
    )
    return 0


def _publish(
    cfg, state: Dict[str, Any], rubric: Rubric, theme: str, series_no: int
) -> int:
    """Боевая сборка и публикация выпуска фото-рубрики."""
    if not cfg.yandex_token or not cfg.yandex_org_id:
        log.error("Для публикации рубрики не заданы YANDEX_FORMS_TOKEN / YANDEX_ORG_ID")
        return 1

    question = photo_flow.question_text(theme)
    need = rubric.questions_count
    log.info(
        "Рубрика «%s» (%s): тема «%s», вопрос «%s», вопросов: %s, порог: %s/%s",
        rubric.id,
        rubric.series_name,
        theme,
        question,
        need,
        rubric.threshold,
        need,
    )

    used_entities = list((state.get("used_entities") or {}).get(theme, []))
    try:
        entities = photo_flow._generate_entities(
            cfg, theme, need + photo_flow.EXTRA_ENTITIES, used_entities
        )
    except Exception as exc:  # noqa: BLE001 - понятная ошибка вместо трейсбека
        log.error("Не удалось получить сущности темы «%s»: %s", theme, exc)
        return 1
    if not entities:
        log.error("Модель не вернула сущностей для темы «%s»", theme)
        return 1

    questions = photo_flow._build_questions(cfg, theme, question, entities, need)
    minimum = cfg.effective_photo_min_questions()
    if len(questions) < minimum:
        log.error(
            "Подготовлено %s вопросов — меньше минимума %s. Публикация отменена.",
            len(questions),
            minimum,
        )
        return 1

    correct_answers = []
    for q in questions:
        options = q.get("options") or []
        index = q.get("correct_index")
        if isinstance(index, int) and 0 <= index < len(options):
            correct_answers.append(str(options[index]))

    title_history = getattr(cfg, "photo_title_history", 10) or 10
    used_titles = list(state.get("used_titles") or [])[-title_history:]
    count = len(questions)

    def title_builder(_used, _answers):
        return build_rubric_title(
            rubric,
            series_no,
            count,
            used_titles=_used,
            correct_answers=_answers,
            theme=theme,
        )

    result = photo_flow.publish_photo_form(
        cfg,
        questions,
        minimum=minimum,
        used_titles=used_titles,
        correct_answers=correct_answers,
        title_builder=title_builder,
        comment_prompt=rubric.comment_prompt,
    )
    if result is None:
        return 1

    base_title = result["base_title"]
    added = result["added"]
    survey_id = result["survey_id"]
    public_url = result["public_url"]

    # Состояние пишем СРАЗУ после публикации формы — до анонса: сбой анонса не
    # должен приводить к повторной публикации той же формы при следующем запуске
    # (повтор в тот же день затем блокирует allow_publish_rubric).
    entry = record_publish(
        state,
        survey_id=survey_id,
        url=public_url,
        rubric_id=rubric.id,
        series_no=series_no,
        threshold=rubric.threshold,
        count=len(added),
    )
    entry["title"] = base_title
    entry["theme"] = theme

    asked = state.setdefault("asked_questions", [])
    asked.extend(f"{q['theme']}: {q['entity']}" for q in added)
    state["asked_questions"] = asked[-photo_flow.ASKED_HISTORY_LIMIT:]

    used_titles_state = state.setdefault("used_titles", [])
    used_titles_state.append(base_title)
    state["used_titles"] = used_titles_state[-photo_flow.TITLE_HISTORY_LIMIT:]

    used_themes = state.setdefault("used_themes", [])
    if theme not in used_themes:
        used_themes.append(theme)

    used_entities_state = state.setdefault("used_entities", {})
    used_entities_state.setdefault(theme, []).extend(q["entity"] for q in added)

    save_state(cfg.rubrics_state_file, state)
    log.info("Состояние сохранено: %s", cfg.rubrics_state_file)

    try:
        posted = publisher.publish_announcement(cfg, survey_id, len(added), base_title)
    except Exception as exc:  # noqa: BLE001 - анонс не должен ронять выпуск
        log.warning("Не удалось опубликовать анонс: %s", exc)
    else:
        if posted:
            log.info("Анонс опубликован: %s", ", ".join(posted))
    return 0


def _publish_plain_form(
    cfg,
    questions: List[Dict[str, Any]],
    minimum: int,
    title_builder,
    comment_prompt: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Создать, наполнить и опубликовать форму БЕЗ картинок — без записи состояния.

    Тот же publish-путь, что и у фото-рубрик (`photo_flow.publish_photo_form`),
    но вопросы — обычные enum без изображений. `title_builder(used_titles,
    correct_answers)` собирает базовый заголовок. Возвращает dict с `survey_id`,
    `public_url`, `base_title`, `added` либо None, если публикация отменена
    (недобор вопросов — черновик формы удаляется).
    """
    # Импорт здесь, чтобы боевые зависимости (клиент Яндекс Форм) не тянулись в
    # dry-run и у тестов оставалась точка monkeypatch-а в rubric_flow.
    from yandex_forms import YandexFormsClient, build_quiz_settings

    correct_answers = []
    for q in questions:
        options = q.get("options") or []
        index = q.get("correct_index")
        if isinstance(index, int) and 0 <= index < len(options):
            correct_answers.append(str(options[index]))

    base_title = title_builder([], correct_answers)

    client = YandexFormsClient(
        cfg.yandex_token, cfg.yandex_org_id, cfg.yandex_org_header
    )

    name = base_title
    if cfg.number_surveys:
        name = client.next_survey_name(name)
    survey_id = client.create_survey(name)
    log.info("Создана форма «%s»: %s", name, survey_id)

    added: List[Dict[str, Any]] = []
    for q in questions:
        try:
            client.add_enum_question(
                survey_id,
                question=q["question"],
                options=q["options"],
                correct_index=q["correct_index"],
                shuffle=cfg.shuffle,
            )
        except Exception as exc:  # noqa: BLE001 - один вопрос не роняет выпуск
            log.warning("Не удалось добавить вопрос «%s»: %s", q["question"], exc)
            continue
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
        return None

    if (comment_prompt or "").strip():
        client.add_comment_question(survey_id, comment_prompt.strip(), header=False)
        log.info("  + вопрос для комментариев (comment)")

    quiz = build_quiz_settings(cfg, total=len(added))
    client.update_survey(
        survey_id, {"quiz": quiz, "stats": cfg.stats, "need_auth": False}
    )
    log.info("Настройки теста применены (stats=%s)", cfg.stats)

    client.set_access(survey_id, access="public", action="submit")
    log.info("Доступ к форме: публичный (заполнение)")

    if cfg.publish:
        client.publish(survey_id)
        log.info("Форма опубликована")

    public_url = YandexFormsClient.public_url(survey_id)
    log.info("Готово. Публичная ссылка: %s", public_url)

    return {
        "survey_id": survey_id,
        "public_url": public_url,
        "base_title": base_title,
        "name": name,
        "added": added,
    }


def _erudite_history(cfg, rubric_state: Dict[str, Any]) -> List[str]:
    """Объединённая история вопросов эрудиционного слота 1 + рубрик.

    Нужна, чтобы вопросы-ловушки не пересекались с основной эрудицией: берём
    `asked_questions` из рубричного состояния и из основного `state.json`
    (там история эрудиционного слота). Файл основного состояния читается
    напрямую; сбой чтения не должен ронять выпуск.
    """
    history: List[str] = list(rubric_state.get("asked_questions") or [])
    state_file = getattr(cfg, "state_file", "")
    if state_file and os.path.exists(state_file):
        try:
            history.extend(load_state(state_file).get("asked_questions") or [])
        except Exception as exc:  # noqa: BLE001 - дедуп по истории не критичен
            log.warning("Не удалось прочитать историю эрудиции из %s: %s", state_file, exc)
    return history


def _collect_trap_questions(
    cfg, rubric: Rubric, checker: DuplicateChecker
) -> List[Dict[str, Any]]:
    """Собрать вопросы-ловушки: генерация с запасом → фактчек → дедуп → отбор.

    Возвращает не больше `rubric.questions_count` вопросов. При сбое генерации
    возвращает уже собранное (может быть пусто — вызывающий проверит минимум).
    """
    import factcheck

    need = rubric.questions_count
    try:
        candidates = generate_trap_questions(cfg, need)
    except Exception as exc:  # noqa: BLE001 - понятная ошибка вместо трейсбека
        log.error("Не удалось получить вопросы-ловушки: %s", exc)
        return []
    log.info("Кандидатов-ловушек от модели: %s", len(candidates))

    kept = factcheck.filter_questions(cfg, candidates)

    collected: List[Dict[str, Any]] = []
    for q in kept:
        if len(collected) >= need:
            break
        if checker.is_duplicate(q["question"]):
            log.info("  повтор, пропускаю: %s", q["question"])
            continue
        checker.add(q["question"])
        collected.append(q)
    return collected


def _trap_dry_run(
    cfg, state: Dict[str, Any], rubric: Rubric, series_no: int
) -> int:
    """Сухая сборка выпуска-ловушки без обращений к API (LLM тоже не вызывается)."""
    log.info(
        "DRY-RUN рубрики «%s» (trap): Яндекс Формы не вызываются, LLM не "
        "вызывается, ловушки-вопросы синтетические.",
        rubric.id,
    )
    need = rubric.questions_count
    log.info(
        "Рубрика «%s» (%s): ловушки без картинок, вопросов: %s, порог: %s/%s",
        rubric.id,
        rubric.series_name,
        need,
        rubric.threshold,
        need,
    )

    correct_answers = [f"верный вариант {i + 1}" for i in range(need)]
    title_history = getattr(cfg, "photo_title_history", 10) or 10
    used_titles = list(state.get("used_titles") or [])[-title_history:]
    title = build_rubric_title(
        rubric,
        series_no,
        need,
        used_titles=used_titles,
        correct_answers=correct_answers,
        wrong_pct=latest_wrong_pct(state, rubric.id),
    )
    log.info("  заголовок: %s", title)

    for i in range(need):
        answer = correct_answers[i]
        log.info("  %2d. Вопрос-ловушка %s: о чём здесь вопрос?", i + 1, i + 1)
        log.info("      правильный ответ: %s", answer)
        for option in (answer, "ловушка 1", "ловушка 2", "ловушка 3"):
            log.info("        %s%s", option, " *" if option == answer else "")

    if (rubric.comment_prompt or "").strip():
        log.info("  + вопрос для комментариев: %s", rubric.comment_prompt.strip())

    log.info(
        "DRY-RUN: будет опубликовано вопросов %s (порог %s), состояние — %s",
        need,
        rubric.threshold,
        cfg.rubrics_state_file,
    )
    return 0


def _trap_publish(
    cfg, state: Dict[str, Any], rubric: Rubric, series_no: int
) -> int:
    """Боевая сборка и публикация выпуска-ловушки (без картинок)."""
    if not cfg.yandex_token or not cfg.yandex_org_id:
        log.error("Для публикации рубрики не заданы YANDEX_FORMS_TOKEN / YANDEX_ORG_ID")
        return 1

    need = rubric.questions_count
    log.info(
        "Рубрика «%s» (%s): ловушки без картинок, вопросов: %s, порог: %s/%s",
        rubric.id,
        rubric.series_name,
        need,
        rubric.threshold,
        need,
    )

    checker = DuplicateChecker(_erudite_history(cfg, state))
    questions = _collect_trap_questions(cfg, rubric, checker)
    if len(questions) < TRAP_MIN_QUESTIONS:
        log.error(
            "Подготовлено %s вопросов-ловушек — меньше минимума %s. "
            "Публикация отменена.",
            len(questions),
            TRAP_MIN_QUESTIONS,
        )
        return 1

    title_history = getattr(cfg, "photo_title_history", 10) or 10
    used_titles = list(state.get("used_titles") or [])[-title_history:]
    count = len(questions)
    wrong_pct = latest_wrong_pct(state, rubric.id)

    def title_builder(_used, _answers):
        return build_rubric_title(
            rubric,
            series_no,
            count,
            used_titles=used_titles,
            correct_answers=_answers,
            wrong_pct=wrong_pct,
        )

    result = _publish_plain_form(
        cfg,
        questions,
        minimum=TRAP_MIN_QUESTIONS,
        title_builder=title_builder,
        comment_prompt=rubric.comment_prompt,
    )
    if result is None:
        return 1

    base_title = result["base_title"]
    added = result["added"]
    survey_id = result["survey_id"]
    public_url = result["public_url"]

    # Состояние пишем СРАЗУ после публикации формы — до анонса: сбой анонса не
    # должен приводить к повторной публикации той же формы при следующем запуске
    # (повтор в тот же день затем блокирует allow_publish_rubric).
    entry = record_publish(
        state,
        survey_id=survey_id,
        url=public_url,
        rubric_id=rubric.id,
        series_no=series_no,
        threshold=rubric.threshold,
        count=len(added),
    )
    entry["title"] = base_title

    asked = state.setdefault("asked_questions", [])
    asked.extend(q["question"] for q in added)
    state["asked_questions"] = asked[-photo_flow.ASKED_HISTORY_LIMIT:]

    used_titles_state = state.setdefault("used_titles", [])
    used_titles_state.append(base_title)
    state["used_titles"] = used_titles_state[-photo_flow.TITLE_HISTORY_LIMIT:]

    save_state(cfg.rubrics_state_file, state)
    log.info("Состояние сохранено: %s", cfg.rubrics_state_file)

    try:
        posted = publisher.publish_announcement(cfg, survey_id, len(added), base_title)
    except Exception as exc:  # noqa: BLE001 - анонс не должен ронять выпуск
        log.warning("Не удалось опубликовать анонс: %s", exc)
    else:
        if posted:
            log.info("Анонс опубликован: %s", ", ".join(posted))
    return 0


def interleave_questions(
    photo_questions: List[Dict[str, Any]],
    text_questions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Равномерно перемешать фото- и текстовые вопросы, начиная с фото.

    Фото встают в позиции `i * total / len(photo)`: при 5 фото и 15 вопросах —
    1-й, 4-й, 7-й, 10-й и 13-й, а при 15 фото и 30 вопросах — все нечётные
    (1, 3, 5, …, 29). Так кадр работает визуальным крючком в начале и
    возвращается по ходу теста, а не сваливается одним блоком.
    """
    photos = list(photo_questions)
    texts = list(text_questions)
    if not photos or not texts:
        return photos + texts

    total = len(photos) + len(texts)
    photo_positions = {i * total // len(photos) for i in range(len(photos))}
    result: List[Dict[str, Any]] = []
    pi = ti = 0
    for pos in range(total):
        if pos in photo_positions and pi < len(photos):
            result.append(photos[pi])
            pi += 1
        elif ti < len(texts):
            result.append(texts[ti])
            ti += 1
        elif pi < len(photos):
            result.append(photos[pi])
            pi += 1
    return result


def _mixed_minimum(rubric: Rubric) -> int:
    """Минимум вопросов смешанного выпуска: не ниже порога + 1 и 80% плана.

    Порог обязан быть достижим даже при недоборе: тест, в котором нельзя
    набрать заявленные `{threshold}`, публиковать нельзя.
    """
    share = math.ceil(rubric.questions_count * MIXED_MIN_RATIO)
    return max(rubric.threshold + 1, share)


def _collect_mixed_questions(
    cfg, state: Dict[str, Any], rubric: Rubric, theme: str
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Собрать фото- и текстовые вопросы одного смешанного выпуска.

    Фото-блок может быть недобран (источник не отдал кадры) — тогда
    недостающее число вопросов добирается текстовыми, чтобы выпуск не
    уменьшался. Обратный недобор (текстовых меньше плана) добирается кадрами.
    Вопросы не должны дублировать друг друга по содержанию: текстовый вопрос,
    упоминающий фильм из кадра, пропускается, как и дополнительный кадр про
    фильм, о котором уже спрашивает текстовый вопрос. Если не собрался ни один
    фото-вопрос или ни один текстовый, возвращаются пустые списки: формат
    рубрики без одной из частей теряет смысл.
    """
    target_total = rubric.questions_count
    photo_target = min(rubric.photo_count, target_total - 1)
    text_target = target_total - photo_target
    question = photo_flow.question_text(theme)

    used_entities = list((state.get("used_entities") or {}).get(theme, []))
    try:
        entities = photo_flow._generate_entities(
            cfg, theme, photo_target + photo_flow.EXTRA_ENTITIES, used_entities
        )
    except Exception as exc:  # noqa: BLE001 - понятная ошибка вместо трейсбека
        log.error("Не удалось получить сущности темы «%s»: %s", theme, exc)
        return [], []
    if not entities:
        log.error("Модель не вернула сущностей для темы «%s»", theme)
        return [], []

    photo_questions = photo_flow._build_questions(
        cfg, theme, question, entities, photo_target
    )
    if not photo_questions:
        log.error(
            "Для смешанной рубрики «%s» не удалось подобрать ни одного кадра — "
            "публикация отменена.",
            rubric.id,
        )
        return [], []
    if len(photo_questions) < photo_target:
        log.warning(
            "Кадров собрано %s из %s — недостающие вопросы доберу текстовыми.",
            len(photo_questions),
            photo_target,
        )
        text_target += photo_target - len(photo_questions)

    avoid = _erudite_history(cfg, state)
    try:
        candidates = generate_topic_questions(
            cfg, theme, text_target, avoid=avoid, extra=_mixed_extra_text(text_target)
        )
    except Exception as exc:  # noqa: BLE001 - понятная ошибка вместо трейсбека
        log.error("Не удалось получить текстовые вопросы по теме «%s»: %s", theme, exc)
        return photo_questions, []
    log.info("Кандидатов текстовых вопросов от модели: %s", len(candidates))

    import factcheck

    candidates = factcheck.filter_questions(cfg, candidates)

    checker = DuplicateChecker(avoid)
    photo_entities = [
        str(q.get("entity") or "").strip()
        for q in photo_questions
        if str(q.get("entity") or "").strip()
    ]
    for entity in photo_entities:
        checker.add(f"{theme}: {entity}")

    text_questions: List[Dict[str, Any]] = []
    for q in candidates:
        if len(text_questions) >= text_target:
            break
        if checker.is_duplicate(q["question"]):
            log.info("  повтор, пропускаю: %s", q["question"])
            continue
        if _mentions_any(q, photo_entities):
            # Текстовый вопрос про тот же фильм, что и кадр, — фактический
            # повтор содержания, хотя формулировки разные (дедуп по тексту его
            # не ловит).
            log.info("  совпадает с кадром по сущности, пропускаю: %s", q["question"])
            continue
        checker.add(q["question"])
        text_questions.append(q)

    if not text_questions:
        log.error(
            "Для смешанной рубрики «%s» не удалось собрать текстовые вопросы — "
            "публикация отменена.",
            rubric.id,
        )
        return photo_questions, []

    # Если текстовых не хватило до плана — добираем оставшимися кадрами.
    shortfall = target_total - (len(photo_questions) + len(text_questions))
    if shortfall > 0:
        used_in_photos = {q.get("entity") for q in photo_questions}
        remaining = [e for e in entities if e not in used_in_photos]
        if remaining:
            extra_photos = photo_flow._build_questions(
                cfg, theme, question, remaining, shortfall
            )
            # Дополнительные кадры не должны повторять фильм, о котором уже
            # спрашивает принятый текстовый вопрос.
            kept_extra = [
                q for q in extra_photos if not _mentions_any_entity(q, text_questions)
            ]
            if kept_extra:
                log.info("Добираю кадрами ещё %s вопросов", len(kept_extra))
                photo_questions.extend(kept_extra)

    return photo_questions, text_questions


def _mentions_any(question: Dict[str, Any], entities: List[str]) -> bool:
    """Упоминает ли вопрос (или его варианты) одну из сущностей-кадров."""
    haystack = _question_haystack(question)
    return any(_contains_phrase(haystack, entity) for entity in entities if entity)


def _mentions_any_entity(
    question: Dict[str, Any], text_questions: List[Dict[str, Any]]
) -> bool:
    """Упоминает ли кадр-сущность один из принятых текстовых вопросов."""
    entity = str(question.get("entity") or "").strip()
    if not entity:
        return False
    return any(_contains_phrase(_question_haystack(text), entity) for text in text_questions)


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """Входит ли фраза в текст как отдельное слово/фраза (не часть слова).

    Границы слова важны: сущность «кот» не должна совпадать со словом
    «который», иначе дедуп выбросит слишком много текстовых вопросов.
    """
    phrase = phrase.strip()
    if not phrase:
        return False
    pattern = r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"
    return re.search(pattern, haystack, flags=re.IGNORECASE) is not None


def _question_haystack(question: Dict[str, Any]) -> str:
    """Текст вопроса и вариантов в нижнем регистре — для проверки упоминаний."""
    parts = [str(question.get("question") or "")]
    parts.extend(str(option) for option in question.get("options") or [])
    return " ".join(parts).lower()


def _mixed_dry_run(
    cfg, state: Dict[str, Any], rubric: Rubric, theme: str, series_no: int
) -> int:
    """Сухая сборка смешанного выпуска без обращений к API."""
    log.info(
        "DRY-RUN рубрики «%s» (mixed): Яндекс Формы не вызываются, изображения "
        "не скачиваются, vision и LLM пропущены.",
        rubric.id,
    )
    total = rubric.questions_count
    photo_count = rubric.photo_count
    text_count = max(0, total - photo_count)
    log.info(
        "Рубрика «%s» (%s): тема «%s», кадров: %s, текстовых: %s, порог: %s/%s",
        rubric.id,
        rubric.series_name,
        theme,
        photo_count,
        text_count,
        rubric.threshold,
        total,
    )

    correct_answers = [f"кадр {i + 1}" for i in range(photo_count)]
    correct_answers += [f"факт {i + 1}" for i in range(text_count)]
    title_history = getattr(cfg, "photo_title_history", 10) or 10
    used_titles = list(state.get("used_titles") or [])[-title_history:]
    title = build_rubric_title(
        rubric,
        series_no,
        total,
        used_titles=used_titles,
        correct_answers=correct_answers,
        theme=theme,
    )
    log.info("  заголовок: %s", title)

    plan = interleave_questions(
        [("фото", f"кадр {i + 1}") for i in range(photo_count)],
        [("текст", f"факт {i + 1}") for i in range(text_count)],
    )
    for i, (kind, item) in enumerate(plan, 1):
        log.info("  %2d. [%s] %s", i, kind, item)

    if (rubric.comment_prompt or "").strip():
        log.info("  + вопрос для комментариев: %s", rubric.comment_prompt.strip())

    log.info(
        "DRY-RUN: будет опубликовано вопросов %s (порог %s), состояние — %s",
        total,
        rubric.threshold,
        cfg.rubrics_state_file,
    )
    return 0


def _mixed_publish(
    cfg, state: Dict[str, Any], rubric: Rubric, theme: str, series_no: int
) -> int:
    """Боевая сборка и публикация смешанного выпуска (кадры + текстовые факты)."""
    if not cfg.yandex_token or not cfg.yandex_org_id:
        log.error("Для публикации рубрики не заданы YANDEX_FORMS_TOKEN / YANDEX_ORG_ID")
        return 1

    log.info(
        "Рубрика «%s» (%s): тема «%s», кадров: %s, текстовых: %s, порог: %s/%s",
        rubric.id,
        rubric.series_name,
        theme,
        rubric.photo_count,
        rubric.questions_count - rubric.photo_count,
        rubric.threshold,
        rubric.questions_count,
    )

    photo_questions, text_questions = _collect_mixed_questions(
        cfg, state, rubric, theme
    )
    if not photo_questions or not text_questions:
        return 1

    questions = interleave_questions(photo_questions, text_questions)
    minimum = _mixed_minimum(rubric)
    if len(questions) < minimum:
        log.error(
            "Подготовлено %s вопросов — меньше минимума %s. Публикация отменена.",
            len(questions),
            minimum,
        )
        return 1
    log.info(
        "Собрано вопросов: %s (кадров %s, текстовых %s)",
        len(questions),
        len(photo_questions),
        len(text_questions),
    )

    correct_answers = []
    for q in questions:
        options = q.get("options") or []
        index = q.get("correct_index")
        if isinstance(index, int) and 0 <= index < len(options):
            correct_answers.append(str(options[index]))

    title_history = getattr(cfg, "photo_title_history", 10) or 10
    used_titles = list(state.get("used_titles") or [])[-title_history:]
    count = len(questions)

    def title_builder(_used, _answers):
        return build_rubric_title(
            rubric,
            series_no,
            count,
            used_titles=_used,
            correct_answers=_answers,
            theme=theme,
        )

    result = photo_flow.publish_photo_form(
        cfg,
        questions,
        minimum=minimum,
        used_titles=used_titles,
        correct_answers=correct_answers,
        title_builder=title_builder,
        comment_prompt=rubric.comment_prompt,
    )
    if result is None:
        return 1

    base_title = result["base_title"]
    added = result["added"]
    survey_id = result["survey_id"]
    public_url = result["public_url"]
    if len(added) != count:
        log.warning(
            "В форму добавлено %s вопросов из %s — заголовок и состояние "
            "фиксируют фактическое число.",
            len(added),
            count,
        )

    # Состояние пишем СРАЗУ после публикации формы — до анонса: сбой анонса не
    # должен приводить к повторной публикации той же формы при следующем запуске.
    entry = record_publish(
        state,
        survey_id=survey_id,
        url=public_url,
        rubric_id=rubric.id,
        series_no=series_no,
        threshold=rubric.threshold,
        count=len(added),
    )
    entry["title"] = base_title
    entry["theme"] = theme
    entry["mixed"] = True
    entry["photo_count"] = sum(1 for q in added if q.get("image"))
    entry["text_count"] = sum(1 for q in added if not q.get("image"))

    asked = state.setdefault("asked_questions", [])
    asked.extend(
        f"{theme}: {q['entity']}" if q.get("entity") else q["question"]
        for q in added
    )
    state["asked_questions"] = asked[-photo_flow.ASKED_HISTORY_LIMIT:]

    used_titles_state = state.setdefault("used_titles", [])
    used_titles_state.append(base_title)
    state["used_titles"] = used_titles_state[-photo_flow.TITLE_HISTORY_LIMIT:]

    used_themes = state.setdefault("used_themes", [])
    if theme not in used_themes:
        used_themes.append(theme)

    used_entities_state = state.setdefault("used_entities", {})
    used_entities_state.setdefault(theme, []).extend(
        q["entity"] for q in added if q.get("entity")
    )

    save_state(cfg.rubrics_state_file, state)
    log.info("Состояние сохранено: %s", cfg.rubrics_state_file)

    try:
        posted = publisher.publish_announcement(cfg, survey_id, len(added), base_title)
    except Exception as exc:  # noqa: BLE001 - анонс не должен ронять выпуск
        log.warning("Не удалось опубликовать анонс: %s", exc)
    else:
        if posted:
            log.info("Анонс опубликован: %s", ", ".join(posted))
    return 0


def run_rubric(
    cfg: Config,
    state: Dict[str, Any],
    rubric_id: str,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """Выпустить один выпуск рубрики. Возвращает код возврата.

    Проверяет разрешение публикации (не чаще раза в UTC-день на рубрику,
    `force` обходит) и диспетчеризует по `source`: `photo` — фото-конвейер,
    `trap` — конвейер вопросов-ловушек (LLM + фактчек, без картинок),
    `mixed` — кадры из фото-конвейера плюс текстовые вопросы по той же теме.
    """
    if cfg.rubrics is None:
        log.error("Реестр рубрик не загружен")
        return 1
    rubric = cfg.rubrics.get(rubric_id)
    if rubric is None:
        log.error("Неизвестная рубрика «%s»", rubric_id)
        return 1
    if not rubric.enabled:
        log.error("Рубрика «%s» выключена (enabled=false)", rubric_id)
        return 1

    if rubric.source not in KNOWN_RUBRIC_SOURCES:
        log.error("Рубрика «%s»: неизвестный source «%s»", rubric_id, rubric.source)
        return 1

    if not dry_run and not allow_publish_rubric(state, rubric.id, force=force):
        log.info(
            "За %s рубрика «%s» уже публиковалась — пропускаю "
            "(используйте --force для повтора).",
            today_utc(),
            rubric.id,
        )
        return 0

    state.setdefault("used_titles", [])
    series_no = next_series_no(state, rubric.id)

    if rubric.source == "trap":
        if dry_run:
            return _trap_dry_run(cfg, state, rubric, series_no)
        return _trap_publish(cfg, state, rubric, series_no)

    if not dry_run and not cfg.photo_flow_enabled:
        # Фото-рубрике и смешанному выпуску нужны источники изображений;
        # фича-флаг фото-потока при этом не обязателен — проверяем только
        # наличие включённых источников.
        if not cfg.enabled_photo_sources():
            log.error(
                "Для рубрики «%s» (source=%s) не включён ни один источник "
                "изображений (PHOTO_SOURCES или кино-флаги).",
                rubric.id,
                rubric.source,
            )
            return 1

    theme = _rubric_theme(cfg, rubric, state)
    if not theme:
        log.error(
            "Не удалось выбрать тему рубрики «%s»: нет доступных тем. "
            "Проверьте PHOTO_THEMES и флаги кино-источников.",
            rubric.id,
        )
        return 1

    if dry_run:
        if rubric.source == "mixed":
            return _mixed_dry_run(cfg, state, rubric, theme, series_no)
        return _dry_run(cfg, state, rubric, theme, series_no)
    if rubric.source == "mixed":
        return _mixed_publish(cfg, state, rubric, theme, series_no)
    return _publish(cfg, state, rubric, theme, series_no)


# Используется main.py для подсказки по доступным id рубрик.
def available_rubric_ids(cfg: Config) -> List[str]:
    if cfg.rubrics is None:
        return []
    return [rubric.id for rubric in cfg.rubrics.enabled_rubrics()]
