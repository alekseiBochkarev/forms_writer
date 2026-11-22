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
    часть — текстовые вопросы. Фото-блок может идти по НЕСКОЛЬКИМ темам
    (`photo_themes` рубрики) с квотами на тему, текстовый — по нескольким
    областям знаний (`text_themes`); без этих списков выпуск собирается по одной
    теме. Вопросы чередуются, чтобы кадры работали визуальными крючками по всей
    форме.
"""

from __future__ import annotations

import copy
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


def _llm_cfg(cfg):
    """Копия конфига для генерации вопросов рубрики.

    Рубрики не поддерживают режим обратной связи (метрики считаются по баллам
    квиза), а завязки в их формах пока не выводятся. Поэтому оба поля у модели
    не запрашиваем: иначе ответы становятся длиннее без пользы для читателя.
    """
    local = copy.copy(cfg)
    local.feedback_enabled = False
    local.question_context_enabled = False
    return local


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
        candidates = generate_trap_questions(_llm_cfg(cfg), need)
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


def _entity_key(entity: Any) -> str:
    """Ключ сравнения сущностей: без регистра и внешних пробелов."""
    return str(entity or "").strip().casefold()


def _split_quota(items: List[str], total: int) -> List[int]:
    """Разложить `total` вопросов по темам: поровну, остаток — первым темам."""
    if not items or total <= 0:
        return [0] * len(items)
    base, rest = divmod(total, len(items))
    return [base + (1 if index < rest else 0) for index in range(len(items))]


def _recent_themes(state: Dict[str, Any]) -> Dict[str, int]:
    """Последний выпуск, в котором встречалась тема: индекс записи `published`.

    Нужен для ротации тем мультитемы: в первую очередь берутся темы, которых
    давно не было. Читается по истории публикаций, чтобы не заводить отдельное
    состояние (и корректно работать со старыми записями без `themes`).
    """
    recent: Dict[str, int] = {}
    published = state.get("published")
    if not isinstance(published, list):
        return recent
    for index, entry in enumerate(published):
        if not isinstance(entry, dict):
            continue
        names: List[str] = []
        themes = entry.get("themes")
        if isinstance(themes, list):
            names.extend(str(theme) for theme in themes)
        label = str(entry.get("theme") or "").strip()
        if label:
            names.append(label)
        for name in names:
            if name:
                recent[name] = index
    return recent


def _rotate_themes(themes: List[str], recent: Dict[str, int]) -> List[str]:
    """Порядок тем выпуска: сначала ни разу не использованные, затем давние.

    Внутри выпуска порядок важен: квоты распределяются сверху вниз, поэтому
    первая тема получает лишний вопрос, а добор начинается с неё же.
    """
    return sorted(themes, key=lambda theme: (theme in recent, recent.get(theme, -1)))


def _mixed_photo_themes(cfg, rubric: Rubric, state: Dict[str, Any]) -> List[str]:
    """Темы фото-блока смешанного выпуска в порядке ротации.

    Явно заданная тема (`PHOTO_THEME`) сужает выпуск до одной темы: ручной запуск
    должен оставаться предсказуемым. Иначе берутся `photo_themes` рубрики (или её
    `themes`), а темы без включённых источников отбрасываются.
    """
    forced = str(getattr(cfg, "photo_theme", "") or "").strip()
    if forced:
        if not photo_flow.available_theme_sources(cfg, forced):
            log.error(
                "Тема фото-блока «%s» недоступна: нет включённых источников "
                "изображений",
                forced,
            )
            return []
        log.info("Тема фото-блока задана явно: %s", forced)
        return [forced]

    candidates = rubric.photo_theme_candidates(cfg.effective_photo_themes())
    if rubric.photo_themes and not candidates:
        log.error(
            "Рубрика «%s»: ни одна из тем %s не найдена в PHOTO_THEMES",
            rubric.id,
            rubric.photo_themes,
        )
        return []
    available: List[str] = []
    for theme in candidates:
        if photo_flow.available_theme_sources(cfg, theme):
            available.append(theme)
        else:
            log.info("Тема «%s» пропущена: нет доступных источников", theme)
    if not available:
        return []
    if len(available) == 1:
        log.info("Рубрика задала единственную тему: %s", available[0])
    return _rotate_themes(available, _recent_themes(state))


def _unique_themes(themes: List[str]) -> List[str]:
    """Темы без пустых значений и повторов (порядок сохраняется)."""
    unique: List[str] = []
    for theme in themes:
        value = str(theme).strip()
        if value and value not in unique:
            unique.append(value)
    return unique


def _mixed_release_label(photo_themes: List[str]) -> str:
    """Подпись выпуска для истории: одна тема как есть, несколько — через запятую."""
    return ", ".join(_unique_themes(photo_themes))


def _mixed_title_theme(photo_themes: List[str]) -> str:
    """Тема для сборки заголовка: у мультитемы категорию не называем (пусто)."""
    unique = _unique_themes(photo_themes)
    return unique[0] if len(unique) == 1 else ""


def _count_values(questions: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    """Сколько вопросов пришлось на каждое значение поля (тема, область знаний)."""
    counts: Dict[str, int] = {}
    for question in questions:
        value = str(question.get(key) or "—")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _format_counts(counts: Dict[str, int]) -> str:
    return ", ".join(f"{name} — {count}" for name, count in counts.items())


def _photo_questions_for_theme(
    cfg, theme: str, count: int, avoid: List[str], seen: set
) -> List[Dict[str, Any]]:
    """Собрать до `count` кадров одной темы, не повторяя сущности из `seen`.

    `seen` — ключи уже занятых выпуском сущностей, в том числе из других тем:
    один и тот же объект может подойти двум темам. Сущность помечается занятой
    сразу, чтобы неудачный поиск картинки не повторялся в доборе.
    """
    if count <= 0:
        return []
    try:
        entities = photo_flow._generate_entities(
            cfg, theme, count + photo_flow.EXTRA_ENTITIES, avoid
        )
    except Exception as exc:  # noqa: BLE001 - понятная ошибка вместо трейсбека
        log.error("Не удалось получить сущности темы «%s»: %s", theme, exc)
        return []
    fresh: List[str] = []
    for entity in entities:
        key = _entity_key(entity)
        if not key or key in seen:
            continue
        seen.add(key)
        fresh.append(entity)
    if not fresh:
        log.warning("Для темы «%s» не осталось новых сущностей", theme)
        return []
    return photo_flow._build_questions(
        cfg, theme, photo_flow.question_text(theme), fresh, count
    )


def _collect_photo_block(
    cfg,
    state: Dict[str, Any],
    themes: List[str],
    need: int,
    exclude: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Фото-блок по нескольким темам: квота на тему плюс проход добора.

    Первый проход выдаёт каждой теме её квоту. Если тема не смогла её закрыть
    (источник не отдал кадры), второй проход даёт каждой теме шанс закрыть общий
    дефицит — выпуск не теряет кадры из-за одной слабой темы.
    """
    if not themes or need <= 0:
        return []
    used_entities = state.get("used_entities") or {}
    seen = {_entity_key(entity) for entity in (exclude or []) if _entity_key(entity)}
    quotas = _split_quota(themes, need)
    questions: List[Dict[str, Any]] = []
    for pass_no in range(2):
        for index, theme in enumerate(themes):
            remaining = need - len(questions)
            if remaining <= 0:
                break
            want = min(quotas[index] if pass_no == 0 else remaining, remaining)
            if want <= 0:
                continue
            avoid = list(used_entities.get(theme) or []) + sorted(seen)
            batch = _photo_questions_for_theme(cfg, theme, want, avoid, seen)
            if not batch:
                continue
            questions.extend(batch)
            log.info("  кадры темы «%s»: %s из %s", theme, len(batch), want)
    return questions[:need]


def _photo_reference_phrases(
    photo_questions: List[Dict[str, Any]],
) -> List[str]:
    """Фразы кадров для проверки пересечений с текстовыми вопросами.

    Для живописи ответ — автор, а картинка ищется по названию, поэтому берём и
    сущность целиком, и ответ, и поисковый запрос: вопрос про любую из этих
    формулировок — фактический повтор содержания кадра.
    """
    phrases: List[str] = []
    for question in photo_questions:
        entity = str(question.get("entity") or "").strip()
        if not entity:
            continue
        theme = str(question.get("theme") or "")
        answer, query = photo_flow.answer_and_query(theme, entity)
        for value in (entity, answer, query):
            text = str(value or "").strip()
            if text and text not in phrases:
                phrases.append(text)
    return phrases


def _collect_text_block(
    cfg,
    state: Dict[str, Any],
    topics: List[str],
    need: int,
    photo_questions: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Текстовый блок по нескольким темам: квота на тему плюс проход добора.

    Каждая область знаний — отдельный запрос к модели: смешение тем внутри
    одного запроса модель понимает плохо и сваливается в одну тему. Кандидаты
    проходят факт-чек и дедупликацию (с историей прошлых выпусков), а вопросы,
    повторяющие содержание кадров, отбрасываются.
    """
    if not topics or need <= 0:
        return []

    import factcheck

    avoid = _erudite_history(cfg, state)
    checker = DuplicateChecker(avoid)
    photo_phrases = _photo_reference_phrases(photo_questions or [])
    for question in photo_questions or []:
        theme = str(question.get("theme") or "")
        entity = str(question.get("entity") or "").strip()
        if entity:
            checker.add(f"{theme}: {entity}")

    quotas = _split_quota(topics, need)
    questions: List[Dict[str, Any]] = []
    for pass_no in range(2):
        for index, topic in enumerate(topics):
            remaining = need - len(questions)
            if remaining <= 0:
                break
            want = min(quotas[index] if pass_no == 0 else remaining, remaining)
            if want <= 0:
                continue
            try:
                candidates = generate_topic_questions(
                    _llm_cfg(cfg), topic, want, avoid=avoid, extra=_mixed_extra_text(want)
                )
            except Exception as exc:  # noqa: BLE001 - один сбой не роняет выпуск
                log.error(
                    "Не удалось получить текстовые вопросы по теме «%s»: %s",
                    topic,
                    exc,
                )
                continue
            log.info(
                "Кандидатов текстовых вопросов по теме «%s»: %s",
                topic,
                len(candidates),
            )
            candidates = factcheck.filter_questions(cfg, candidates)
            taken = 0
            for question in candidates:
                if len(questions) >= need or taken >= want:
                    break
                if checker.is_duplicate(question["question"]):
                    log.info("  повтор, пропускаю: %s", question["question"])
                    continue
                if _mentions_any(question, photo_phrases):
                    # Вопрос про тот же объект, что и кадр, — фактический повтор
                    # содержания, хотя формулировки разные (дедуп по тексту его
                    # не ловит).
                    log.info(
                        "  совпадает с кадром по содержанию, пропускаю: %s",
                        question["question"],
                    )
                    continue
                checker.add(question["question"])
                questions.append(question)
                taken += 1
            if not taken:
                log.info("  тема «%s» не дала новых вопросов", topic)
    return questions[:need]


def _collect_mixed_questions(
    cfg,
    state: Dict[str, Any],
    rubric: Rubric,
    photo_themes: List[str],
    text_topics: List[str],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Собрать фото- и текстовые вопросы одного смешанного выпуска.

    Фото-блок идёт по нескольким темам с квотами, текстовый — по нескольким
    областям знаний. Фото-блок может быть недобран (источник не отдал кадры) —
    тогда недостающее число вопросов добирается текстовыми, чтобы выпуск не
    уменьшался. Обратный недобор (текстовых меньше плана) добирается кадрами.
    Вопросы не должны дублировать друг друга по содержанию. Если не собрался ни
    один фото-вопрос или ни один текстовый, возвращаются пустые списки: формат
    рубрики без одной из частей теряет смысл.
    """
    target_total = rubric.questions_count
    photo_target = min(rubric.photo_count, target_total - 1)
    text_target = target_total - photo_target

    photo_questions = _collect_photo_block(cfg, state, photo_themes, photo_target)
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

    text_questions = _collect_text_block(
        cfg, state, text_topics, text_target, photo_questions
    )
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
        extra_photos = _collect_photo_block(
            cfg,
            state,
            photo_themes,
            shortfall,
            exclude=[q.get("entity") for q in photo_questions],
        )
        # Дополнительные кадры не должны повторять то, о чём уже спрашивает
        # принятый текстовый вопрос.
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
    cfg,
    state: Dict[str, Any],
    rubric: Rubric,
    photo_themes: List[str],
    text_topics: List[str],
    series_no: int,
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
        "Рубрика «%s» (%s): фото-темы: %s; текстовые темы: %s; кадров: %s, "
        "текстовых: %s, порог: %s/%s",
        rubric.id,
        rubric.series_name,
        ", ".join(photo_themes) or "—",
        ", ".join(text_topics) or "—",
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
        theme=_mixed_title_theme(photo_themes),
    )
    log.info("  заголовок: %s", title)

    photo_plan = [
        (f"фото: {theme}", f"кадр {index + 1}")
        for theme, quota in zip(
            photo_themes, _split_quota(photo_themes, photo_count)
        )
        for index in range(quota)
    ]
    text_plan = [
        (f"текст: {topic}", f"факт {index + 1}")
        for topic, quota in zip(text_topics, _split_quota(text_topics, text_count))
        for index in range(quota)
    ]
    plan = interleave_questions(photo_plan, text_plan)
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
    cfg,
    state: Dict[str, Any],
    rubric: Rubric,
    photo_themes: List[str],
    text_topics: List[str],
    series_no: int,
) -> int:
    """Боевая сборка и публикация смешанного выпуска (кадры + текстовые факты)."""
    if not cfg.yandex_token or not cfg.yandex_org_id:
        log.error("Для публикации рубрики не заданы YANDEX_FORMS_TOKEN / YANDEX_ORG_ID")
        return 1

    label = _mixed_release_label(photo_themes)
    log.info(
        "Рубрика «%s» (%s): фото-темы: %s; текстовые темы: %s; кадров: %s, "
        "текстовых: %s, порог: %s/%s",
        rubric.id,
        rubric.series_name,
        label or "—",
        ", ".join(text_topics) or "—",
        rubric.photo_count,
        rubric.questions_count - rubric.photo_count,
        rubric.threshold,
        rubric.questions_count,
    )

    photo_questions, text_questions = _collect_mixed_questions(
        cfg, state, rubric, photo_themes, text_topics
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
    log.info(
        "Раскладка по фото-темам: %s",
        _format_counts(_count_values(photo_questions, "theme")),
    )
    log.info(
        "Раскладка по текстовым темам: %s",
        _format_counts(_count_values(text_questions, "topic")),
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
    title_theme = _mixed_title_theme(photo_themes)

    def title_builder(_used, _answers):
        return build_rubric_title(
            rubric,
            series_no,
            count,
            used_titles=_used,
            correct_answers=_answers,
            theme=title_theme,
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
    # Для мультитемы `theme` — подпись выпуска, а список тем — в `themes`.
    entry["theme"] = label
    entry["themes"] = list(photo_themes)
    if list(text_topics) != list(photo_themes):
        entry["text_themes"] = list(text_topics)
    entry["mixed"] = True
    entry["photo_count"] = sum(1 for q in added if q.get("image"))
    entry["text_count"] = sum(1 for q in added if not q.get("image"))

    asked = state.setdefault("asked_questions", [])
    asked.extend(
        f"{q.get('theme') or label}: {q['entity']}"
        if q.get("entity")
        else q["question"]
        for q in added
    )
    state["asked_questions"] = asked[-photo_flow.ASKED_HISTORY_LIMIT:]

    used_titles_state = state.setdefault("used_titles", [])
    used_titles_state.append(base_title)
    state["used_titles"] = used_titles_state[-photo_flow.TITLE_HISTORY_LIMIT:]

    used_themes = state.setdefault("used_themes", [])
    if len(photo_themes) == 1 and photo_themes[0] not in used_themes:
        # История тем фото-потока — прежняя семантика для однотемных выпусков.
        # Ротация мультитемы считается по `published[]`, поэтому здесь список
        # не засоряется несколькими темами одного выпуска.
        used_themes.append(photo_themes[0])

    used_entities_state = state.setdefault("used_entities", {})
    for question in added:
        entity = question.get("entity")
        if not entity:
            continue
        theme_of = str(question.get("theme") or label).strip()
        used_entities_state.setdefault(theme_of, []).append(entity)

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
    `mixed` — кадры из фото-конвейера плюс текстовые вопросы (по одной теме
    или по нескольким — см. `photo_themes`/`text_themes` рубрики).
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

    if rubric.source == "mixed":
        # Смешанный выпуск может идти по нескольким темам сразу: фото-блок — с
        # квотами по темам, текстовый — по областям знаний. Одна тема (рубрика
        # «d») сохраняет прежнее поведение.
        photo_themes = _mixed_photo_themes(cfg, rubric, state)
        if not photo_themes:
            log.error(
                "Не удалось выбрать темы фото-блока рубрики «%s»: нет доступных "
                "тем. Проверьте PHOTO_THEMES, RUBRICS и флаги кино-источников.",
                rubric.id,
            )
            return 1
        label = _mixed_release_label(photo_themes)
        text_topics = rubric.text_topic_candidates(label)
        if not text_topics:
            log.error(
                "Рубрика «%s»: не заданы темы текстового блока (themes/"
                "text_themes) — публикация отменена.",
                rubric.id,
            )
            return 1
        if dry_run:
            return _mixed_dry_run(
                cfg, state, rubric, photo_themes, text_topics, series_no
            )
        return _mixed_publish(
            cfg, state, rubric, photo_themes, text_topics, series_no
        )

    theme = _rubric_theme(cfg, rubric, state)
    if not theme:
        log.error(
            "Не удалось выбрать тему рубрики «%s»: нет доступных тем. "
            "Проверьте PHOTO_THEMES и флаги кино-источников.",
            rubric.id,
        )
        return 1

    if dry_run:
        return _dry_run(cfg, state, rubric, theme, series_no)
    return _publish(cfg, state, rubric, theme, series_no)


# Используется main.py для подсказки по доступным id рубрик.
def available_rubric_ids(cfg: Config) -> List[str]:
    if cfg.rubrics is None:
        return []
    return [rubric.id for rubric in cfg.rubrics.enabled_rubrics()]
