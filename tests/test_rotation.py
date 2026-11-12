"""Ротация выпусков: эрудиция каждый день плюс два потока из пула."""

from __future__ import annotations

from datetime import date, timedelta

import rotation


def test_erudition_is_always_scheduled():
    """Обязательный поток выходит в любой день."""
    start = date(2026, 1, 1)
    for offset in range(0, 365, 7):
        day = start + timedelta(days=offset)
        assert rotation.is_scheduled("erudition", day)
        assert "erudition" in rotation.planned_flows(day)


def test_pool_and_mandatory_flows_are_separate():
    """Пул дополнительных потоков не пересекается с обязательными."""
    assert not set(rotation.MANDATORY_FLOWS) & set(rotation.EXTRA_FLOWS)
    assert len(rotation.EXTRA_FLOWS) == 5
    assert rotation.EXTRA_PER_DAY == 2


def test_three_flows_per_day_and_extras_unique():
    """В день ровно три теста, и все они разные."""
    start = date(2026, 1, 1)
    for offset in range(365):
        day = start + timedelta(days=offset)
        flows = rotation.planned_flows(day)
        assert len(flows) == len(rotation.MANDATORY_FLOWS) + rotation.EXTRA_PER_DAY
        assert len(set(flows)) == len(flows)
        assert flows[0] == "erudition"
        assert set(flows[1:]) <= set(rotation.EXTRA_FLOWS)


def test_each_extra_flow_runs_twice_per_five_days():
    """Цикл ротации: за пять дней каждый поток пула выходит ровно дважды."""
    start = date(2026, 1, 5)
    counters = {flow: 0 for flow in rotation.EXTRA_FLOWS}
    for offset in range(5):
        for flow in rotation.planned_extras(start + timedelta(days=offset)):
            counters[flow] += 1

    assert counters == {flow: 2 for flow in rotation.EXTRA_FLOWS}
    assert sum(counters.values()) == 5 * rotation.EXTRA_PER_DAY


def test_daily_pairs_do_not_repeat_within_cycle():
    """Пары потоков внутри цикла не повторяются."""
    start = date(2026, 1, 5)
    pairs = [
        tuple(rotation.planned_extras(start + timedelta(days=offset))) for offset in range(5)
    ]

    assert all(len(pair) == rotation.EXTRA_PER_DAY for pair in pairs)
    assert len(set(pairs)) == len(pairs)


def test_is_scheduled_matches_plan():
    """`is_scheduled` согласован с планом на любую дату."""
    start = date(2026, 3, 1)
    for offset in range(30):
        day = start + timedelta(days=offset)
        planned = set(rotation.planned_flows(day))
        for flow in (*rotation.MANDATORY_FLOWS, *rotation.EXTRA_FLOWS):
            assert rotation.is_scheduled(flow, day) is (flow in planned)


def test_unknown_flow_is_not_scheduled():
    """Потоки вне пула (например, отключённая рубрика «a») не запускаются."""
    assert rotation.is_scheduled("rubric:a", date(2026, 5, 1)) is False


def test_default_date_is_today_utc():
    """Без аргумента план строится на сегодняшнюю дату UTC."""
    assert rotation.planned_flows() == rotation.planned_flows(rotation.today_utc())


def test_pool_rubrics_exist_in_config():
    """Все рубрики из пула ротации реально есть в реестре рубрик."""
    from config import DEFAULT_RUBRICS

    known = {item["id"] for item in DEFAULT_RUBRICS}
    from_pool = {
        flow.split(":", 1)[1] for flow in rotation.EXTRA_FLOWS if flow.startswith("rubric:")
    }

    assert from_pool
    assert from_pool <= known


def test_cli_prints_plan(capsys):
    """CLI печатает план на запрошенную дату."""
    day = date(2026, 11, 12)

    assert rotation.main(["--date", day.isoformat()]) == 0
    out = capsys.readouterr().out
    assert day.isoformat() in out
    assert "эрудиция" in out


def test_cli_exit_code_for_scheduled_flag(capsys):
    """`--is-scheduled` возвращает 0 для потока из плана и 1 для остальных."""
    day = date(2026, 11, 12)
    planned = rotation.planned_extras(day)
    skipped = next(flow for flow in rotation.EXTRA_FLOWS if flow not in planned)

    assert rotation.main(["--date", day.isoformat(), "--is-scheduled", "erudition"]) == 0
    for flow in planned:
        assert rotation.main(["--date", day.isoformat(), "--is-scheduled", flow]) == 0
    assert rotation.main(["--date", day.isoformat(), "--is-scheduled", skipped]) == 1
    capsys.readouterr()
