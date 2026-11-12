"""Календарь ежедневных выпусков: эрудиция всегда плюс два потока по ротации.

GitHub Actions запускает workflow по фиксированным cron, а какие именно
дополнительные выпуски выходят сегодня, решает этот модуль по номеру дня UTC.
Ротация детерминированная: её видно заранее, и она не зависит от того, успел ли
предыдущий job.

Пул из пяти кандидатов идёт в фиксированном порядке, поэтому за пять дней
каждый выходит ровно дважды, а эрудиция — каждый день.

Примеры:
    python rotation.py                        # план на сегодня (UTC)
    python rotation.py --date 2026-11-12      # план на конкретную дату
    python rotation.py --is-scheduled photo   # код 0, если поток выходит сегодня
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from typing import List, Optional

# Ежедневные выпуски: этот выходит всегда.
MANDATORY_FLOWS = ("erudition",)

# Пул дополнительных потоков. Порядок фиксирован — он задаёт цикл ротации.
EXTRA_FLOWS = (
    "new_tests",
    "rubric:b",
    "rubric:d",
    "rubric:e",
    "photo",
)

# Сколько дополнительных выпусков в день (плюс обязательная эрудиция).
EXTRA_PER_DAY = 2

FLOW_LABELS = {
    "erudition": "эрудиция (общая, 13 вопросов)",
    "new_tests": "новые тесты (15 вопросов, 10/15)",
    "rubric:b": "рубрика b — Знаток по фото (10/8)",
    "rubric:d": "рубрика d — Кино: кадры и факты (15/10)",
    "rubric:e": "рубрика e — Эрудит: фото и факты (30/20)",
    "photo": "фото-тест (10 вопросов с картинками)",
}


def today_utc() -> date:
    """Текущая дата по UTC — по ней же считаются cron-слоты GitHub Actions."""
    return datetime.now(timezone.utc).date()


def planned_extras(day: date) -> List[str]:
    """Два дополнительных потока на дату.

    Сдвиг на два элемента в день: за пять дней пул проходится дважды, пары
    не повторяются, а внутри дня потоки всегда разные.
    """
    start = EXTRA_PER_DAY * day.toordinal()
    return [EXTRA_FLOWS[(start + i) % len(EXTRA_FLOWS)] for i in range(EXTRA_PER_DAY)]


def planned_flows(day: Optional[date] = None) -> List[str]:
    """Полный план на дату: обязательные выпуски и ротация."""
    day = day or today_utc()
    return [*MANDATORY_FLOWS, *planned_extras(day)]


def is_scheduled(flow: str, day: Optional[date] = None) -> bool:
    """Выходит ли поток в эту дату."""
    if flow in MANDATORY_FLOWS:
        return True
    return flow in planned_extras(day or today_utc())


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="План ежедневных выпусков тестов")
    parser.add_argument("--date", help="дата в формате YYYY-MM-DD (по умолчанию — сегодня UTC)")
    parser.add_argument(
        "--is-scheduled",
        metavar="FLOW",
        help="проверить поток (erudition/new_tests/rubric:b/photo) и выйти",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    day = date.fromisoformat(args.date) if args.date else today_utc()

    if args.is_scheduled:
        return 0 if is_scheduled(args.is_scheduled, day) else 1

    print(f"План на {day.isoformat()} (UTC):")
    for number, flow in enumerate(planned_flows(day), 1):
        print(f"  {number}. {FLOW_LABELS.get(flow, flow)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
