"""Ротация выпусков: эрудиция и кино каждый день плюс один поток из пула."""

from __future__ import annotations

from datetime import date, timedelta

import rotation


def test_mandatory_flows_are_always_scheduled():
    """Обязательные потоки (эрудиция и кино) выходят в любой день."""
    start = date(2026, 1, 1)
    for offset in range(0, 365, 7):
        day = start + timedelta(days=offset)
        for flow in rotation.MANDATORY_FLOWS:
            assert rotation.is_scheduled(flow, day)
            assert flow in rotation.planned_flows(day)
        # Кино — ежедневная рубрика, это зафиксировано отдельной проверкой.
        assert rotation.is_scheduled("rubric:d", day)


def test_pool_and_mandatory_flows_are_separate():
    """Пул дополнительных потоков не пересекается с обязательными."""
    assert not set(rotation.MANDATORY_FLOWS) & set(rotation.EXTRA_FLOWS)
    assert len(rotation.EXTRA_FLOWS) == 4
    assert rotation.EXTRA_PER_DAY == 1


def test_three_flows_per_day_and_extras_unique():
    """В день ровно три теста, и все они разные."""
    start = date(2026, 1, 1)
    for offset in range(365):
        day = start + timedelta(days=offset)
        flows = rotation.planned_flows(day)
        assert len(flows) == len(rotation.MANDATORY_FLOWS) + rotation.EXTRA_PER_DAY
        assert len(flows) == 3
        assert len(set(flows)) == len(flows)
        assert flows[0] == "erudition"
        assert flows[1] == "rubric:d"
        assert set(flows[2:]) <= set(rotation.EXTRA_FLOWS)


def test_each_extra_flow_runs_once_per_four_days():
    """Цикл ротации: за четыре дня каждый поток пула выходит ровно раз."""
    start = date(2026, 1, 5)
    counters = {flow: 0 for flow in rotation.EXTRA_FLOWS}
    for offset in range(len(rotation.EXTRA_FLOWS)):
        for flow in rotation.planned_extras(start + timedelta(days=offset)):
            counters[flow] += 1

    assert counters == {flow: 1 for flow in rotation.EXTRA_FLOWS}
    assert sum(counters.values()) == len(rotation.EXTRA_FLOWS) * rotation.EXTRA_PER_DAY


def test_daily_extras_do_not_repeat_within_cycle():
    """Дополнительные потоки внутри цикла не повторяются."""
    start = date(2026, 1, 5)
    cycle = len(rotation.EXTRA_FLOWS) // rotation.EXTRA_PER_DAY
    extras = [
        tuple(rotation.planned_extras(start + timedelta(days=offset)))
        for offset in range(cycle)
    ]

    assert all(len(pair) == rotation.EXTRA_PER_DAY for pair in extras)
    assert len(set(extras)) == len(extras)


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


def test_rotation_rubrics_exist_in_config():
    """Все рубрики из ротации (пул и обязательные) реально есть в реестре."""
    from config import DEFAULT_RUBRICS

    known = {item["id"] for item in DEFAULT_RUBRICS}
    from_rotation = {
        flow.split(":", 1)[1]
        for flow in (*rotation.MANDATORY_FLOWS, *rotation.EXTRA_FLOWS)
        if flow.startswith("rubric:")
    }

    assert from_rotation
    assert from_rotation <= known


def test_cli_prints_plan(capsys):
    """CLI печатает план на запрошенную дату."""
    day = date(2026, 11, 12)

    assert rotation.main(["--date", day.isoformat()]) == 0
    out = capsys.readouterr().out
    assert day.isoformat() in out
    assert "эрудиция" in out
    assert "Кино" in out


def test_cli_exit_code_for_scheduled_flag(capsys):
    """`--is-scheduled` возвращает 0 для потока из плана и 1 для остальных."""
    day = date(2026, 11, 12)
    planned = rotation.planned_extras(day)
    skipped = next(flow for flow in rotation.EXTRA_FLOWS if flow not in planned)

    assert rotation.main(["--date", day.isoformat(), "--is-scheduled", "erudition"]) == 0
    assert rotation.main(["--date", day.isoformat(), "--is-scheduled", "rubric:d"]) == 0
    for flow in planned:
        assert rotation.main(["--date", day.isoformat(), "--is-scheduled", flow]) == 0
    assert rotation.main(["--date", day.isoformat(), "--is-scheduled", skipped]) == 1
    capsys.readouterr()
