"""Метрики эксперимента с рубриками: число ответов и доля ошибшихся.

Решение по метрике эксперимента — «медиана числа ответов формы за 48 часов» как
прокси просмотров (API Яндекс Форм просмотры не отдаёт). Скрипт для каждой
свежей публикации рубрики берёт статистику ответов формы (ручка `/answers`,
как в `hard_questions.py`), считает число ответов и долю верных, а из них —
долю ошибшихся `wrong_pct`. Это значение подхватывает `rubric_flow.latest_wrong_pct`
и подставляет в плейсхолдер `{wrong_pct}` шаблонов заголовков рубрик-ловушек.

После обновления метрик печатает сводку по рубрикам: число публикаций, медиану
числа ответов за последние 7 дней, средний `wrong_pct` и пометку «ниже порога»,
если медиана меньше `RUBRIC_MIN_MEDIAN` (это число ОТВЕТОВ — прокси просмотров).

Примеры:
    python rubric_metrics.py                       # обновить и показать отчёт
    python rubric_metrics.py --hours 48            # свой порог «свежести»
    python rubric_metrics.py --state rubrics_state.json --json
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import DEFAULT_RUBRIC_MIN_MEDIAN, load_config
from hard_questions import _answered, _is_correct, _iter_answers, _question_meta
from state import load_state, now_utc_iso, save_state
from yandex_forms import YandexFormsClient

log = logging.getLogger("rubric_metrics")

# Медиана считается по публикациям за последние N суток.
REPORT_WINDOW_DAYS = 7


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Метрики рубрик: число ответов и доля ошибшихся по формам"
    )
    parser.add_argument(
        "--state",
        help="файл состояния рубрик (по умолчанию RUBRICS_STATE_FILE / rubrics_state.json)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=48,
        help="обновлять публикации старше N часов (по умолчанию 48)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="вывести отчёт в JSON вместо таблицы",
    )
    return parser.parse_args()


def _parse_ts(raw: Any) -> Optional[datetime]:
    """Разобрать ISO-метку времени (publish_ts/metrics_ts) в aware-datetime UTC."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _needs_metrics(entry: Dict[str, Any]) -> bool:
    """Нужно ли обновить метрики записи: нет `metrics_checked` и пустой `views_checkpoints`."""
    if entry.get("metrics_checked"):
        return False
    checkpoints = entry.get("views_checkpoints")
    if isinstance(checkpoints, list) and checkpoints:
        return False
    return True


def _is_stale(entry: Dict[str, Any], now: datetime, hours: int) -> bool:
    """Опубликовано ли достаточно давно (старше `hours` часов по publish_ts)."""
    ts = _parse_ts(entry.get("publish_ts") or entry.get("date"))
    if ts is None:
        # Без внятной метки считаем запись старой — метрику получить можно.
        return True
    return ts <= now - timedelta(hours=hours)


def compute_metrics(client: YandexFormsClient, survey_id: str) -> Dict[str, Any]:
    """Посчитать для формы число ответов и долю верных.

    Переиспользует счётчики `hard_questions`: те же `_iter_answers` (ручка
    `/answers` с постраничным обходом) и `_question_meta`/`_answered`/`_is_correct`
    для оценки верности ответа. `answers_count` — число респондентов, оставивших
    хотя бы один ответ на вопрос викторины. `correct_rate` — доля верных ответов
    по всем вопросам формы.
    """
    questions = client.get_questions(survey_id)
    meta = _question_meta(questions)
    answered_by_q: Dict[int, int] = {qid: 0 for qid in meta}
    correct_by_q: Dict[int, int] = {qid: 0 for qid in meta}
    respondents = 0

    for answer in _iter_answers(client, survey_id):
        data = answer.get("data") or []
        columns = answer.get("_columns") or []
        touched = False
        for idx, entry in enumerate(data):
            if not _answered(entry):
                continue
            if idx >= len(columns):
                continue
            qid = columns[idx]
            if qid not in meta:
                continue
            touched = True
            answered_by_q[qid] += 1
            if _is_correct(entry, meta[qid]["correct_labels"]):
                correct_by_q[qid] += 1
        if touched:
            respondents += 1

    total_answered = sum(answered_by_q.values())
    total_correct = sum(correct_by_q.values())
    correct_rate = total_correct / total_answered if total_answered else 0.0
    return {
        "answers_count": respondents,
        "correct_rate": correct_rate,
    }


def refresh_metrics(
    client: YandexFormsClient,
    state: Dict[str, Any],
    hours: int = 48,
    now: Optional[datetime] = None,
) -> int:
    """Обновить метрики свежих публикаций рубрик. Возвращает число обновлённых записей.

    Для каждой записи `published[]` старше `hours` часов, у которой ещё нет
    метрик, пишет `answers_count`, `correct_rate`, `wrong_pct` и `metrics_ts`.
    Сбой по одной форме не роняет весь прогон — запись остаётся без метрик
    (будет попытана в следующий раз).
    """
    now = now or datetime.now(timezone.utc)
    published = state.get("published")
    if not isinstance(published, list):
        return 0

    updated = 0
    for entry in published:
        if not isinstance(entry, dict):
            continue
        if entry.get("rubric_id") is None:
            continue
        if not _needs_metrics(entry) or not _is_stale(entry, now, hours):
            continue
        survey_id = str(entry.get("survey_id") or "")
        if not survey_id:
            continue
        try:
            metrics = compute_metrics(client, survey_id)
        except Exception as exc:  # noqa: BLE001 - одна форма не роняет весь прогон
            log.warning("Форма %s: не удалось получить метрики (%s)", survey_id, exc)
            continue
        correct_rate = metrics["correct_rate"]
        entry["answers_count"] = metrics["answers_count"]
        entry["correct_rate"] = round(correct_rate, 4)
        entry["wrong_pct"] = round((1 - correct_rate) * 100)
        entry["metrics_ts"] = now_utc_iso()
        updated += 1
        log.info(
            "Форма %s (рубрика «%s»): ответов %s, верных %.0f%%, ошиблись %s%%",
            survey_id,
            entry.get("rubric_id"),
            entry["answers_count"],
            correct_rate * 100,
            entry["wrong_pct"],
        )
    return updated


def build_report(
    state: Dict[str, Any],
    min_median: int = DEFAULT_RUBRIC_MIN_MEDIAN,
    now: Optional[datetime] = None,
    window_days: int = REPORT_WINDOW_DAYS,
) -> List[Dict[str, Any]]:
    """Собрать построчный отчёт по рубрикам.

    Для каждого `rubric_id`: число публикаций, медиана `answers_count` по
    публикациям за последние `window_days` (с уже посчитанными метриками),
    средний `wrong_pct` по всем публикациям с метрикой и флаг `below_threshold`
    (медиана меньше `min_median`).

    Репак-потомки (записи с `repack_of`) исключены из медианы и среднего
    `wrong_pct`: репак — отдельное событие (перевыпуск тех же вопросов), а не
    самостоятельная публикация серии, иначе провал считался бы дважды.
    """
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)
    published = state.get("published")
    if not isinstance(published, list):
        return []

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for entry in published:
        if not isinstance(entry, dict):
            continue
        rubric_id = entry.get("rubric_id")
        if rubric_id is None:
            continue
        grouped.setdefault(str(rubric_id), []).append(entry)

    rows: List[Dict[str, Any]] = []
    for rubric_id in sorted(grouped):
        entries = grouped[rubric_id]
        answers_recent: List[float] = []
        wrong_values: List[float] = []
        for entry in entries:
            if entry.get("repack_of"):
                continue
            answers = entry.get("answers_count")
            if isinstance(answers, (int, float)):
                ts = _parse_ts(entry.get("publish_ts") or entry.get("date"))
                if ts is not None and ts >= window_start:
                    answers_recent.append(float(answers))
            wrong = entry.get("wrong_pct")
            if isinstance(wrong, (int, float)):
                wrong_values.append(float(wrong))

        median_answers = statistics.median(answers_recent) if answers_recent else None
        avg_wrong = sum(wrong_values) / len(wrong_values) if wrong_values else None
        below = (
            median_answers is not None and median_answers < min_median
        )
        rows.append(
            {
                "rubric_id": rubric_id,
                "publications": len(entries),
                "median_answers_7d": median_answers,
                "avg_wrong_pct": avg_wrong,
                "below_threshold": below,
            }
        )
    return rows


def _fmt(value: Optional[float], digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def render_text(rows: List[Dict[str, Any]], min_median: int) -> str:
    """Отпечатать отчёт текстовой таблицей."""
    if not rows:
        return "Публикаций рубрик с метриками пока нет."
    header = (
        f"{'РУБРИКА':<9}{'ПУБЛ':>6}{'МЕДИАНА(7д)':>14}"
        f"{'СРЕД.ОШИБ,%':>14}   ВЕРДИКТ"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        verdict = "ниже порога" if row["below_threshold"] else "ок"
        lines.append(
            f"{row['rubric_id']:<9}"
            f"{row['publications']:>6}"
            f"{_fmt(row['median_answers_7d']):>14}"
            f"{_fmt(row['avg_wrong_pct']):>14}   {verdict}"
        )
    lines.append("")
    lines.append(
        f"Порог медианы (число ОТВЕТОВ, прокси просмотров): {min_median}"
    )
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()

    cfg = load_config({"dry_run": True}, require_questions=False)
    state_file = args.state or cfg.rubrics_state_file
    min_median = cfg.rubric_min_median

    state = load_state(state_file)

    if not cfg.yandex_token or not cfg.yandex_org_id:
        log.error(
            "Нужны YANDEX_FORMS_TOKEN / YANDEX_ORG_ID для чтения ответов форм — "
            "метрики не обновляются, показан отчёт по уже известным данным."
        )
    else:
        client = YandexFormsClient(
            cfg.yandex_token, cfg.yandex_org_id, cfg.yandex_org_header
        )
        updated = refresh_metrics(client, state, hours=args.hours)
        if updated:
            save_state(state_file, state)
            log.info("Обновлено записей с метриками: %s → %s", updated, state_file)
        else:
            log.info("Новых записей для обновления метрик нет")

    rows = build_report(state, min_median=min_median)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(render_text(rows, min_median))
    return 0


if __name__ == "__main__":
    sys.exit(main())
