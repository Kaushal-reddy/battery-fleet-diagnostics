"""Unit tests for the CAN checks, on small hand-built tables (never the simulator output)."""

import numpy as np
import pandas as pd

from bms_can.checks import (check_bms_flags, check_flag_consistency, check_stuck, check_timeout,
                            mask_to_intervals)

DT = 0.1


def times(n: int, start: float = 0.0) -> np.ndarray:
    """n timestamps spaced DT apart, rounded to avoid float noise."""
    return np.round(start + np.arange(n) * DT, 6)


# mask_to_intervals

def test_intervals_all_false() -> None:
    """No true samples gives no intervals."""
    assert mask_to_intervals(times(10), [False] * 10) == []


def test_intervals_one_block() -> None:
    """One block of true samples gives one interval from its first to last true time."""
    mask = [False, True, True, True, False]
    assert mask_to_intervals(times(5), mask) == [(0.1, 0.3)]


def test_intervals_two_blocks() -> None:
    """Two separate blocks give two intervals, including one touching the end."""
    mask = [True, True, False, False, True, True]
    assert mask_to_intervals(times(6), mask) == [(0.0, 0.1), (0.4, 0.5)]


def test_intervals_short_block_dropped() -> None:
    """A block shorter than min_duration is dropped; a longer one is kept."""
    mask = [True, True, False, True, True, True, True, True, True]  # 0.1 s block, then 0.5 s block
    assert mask_to_intervals(times(9), mask, min_duration=0.3) == [(0.3, 0.8)]


# check_timeout

def test_timeout_one_gap() -> None:
    """Regular 0.1 s frames with one 0.5 s gap give exactly one event spanning the gap."""
    t = np.concatenate([times(10), times(10, start=1.4)])  # last frame 0.9 s, next 1.4 s
    events = check_timeout(pd.DataFrame({"t": t}), "BMS_Status", DT)
    assert len(events) == 1
    assert (events[0].t_start, events[0].t_end) == (0.9, 1.4)


def test_timeout_no_gap() -> None:
    """Regular 0.1 s frames give no events."""
    assert check_timeout(pd.DataFrame({"t": times(50)}), "BMS_Status", DT) == []


# check_stuck

def stuck_status(current_a: float) -> pd.DataFrame:
    """60 s of BMS_Status with soc_pct frozen at 50 % and a constant current."""
    n = 600
    return pd.DataFrame({"t": times(n), "soc_pct": 50.0, "pack_current_a": current_a, "pack_voltage_v": 800.0})


def test_stuck_high_current() -> None:
    """Flat SoC for 60 s at -250 A implies ~2 % change on 200 Ah, so it is flagged."""
    events = check_stuck(stuck_status(-250.0))
    assert len(events) == 1
    assert events[0].value > 2.0


def test_stuck_zero_current() -> None:
    """Flat SoC at rest is expected, so it is not flagged."""
    assert check_stuck(stuck_status(0.0)) == []


# check_flag_consistency

def uv_case(seconds_low: float) -> list:
    """5 s of data with voltage below a 720 V limit for the given time and the UV flag stuck at 0."""
    t = times(50)
    low = (t >= 1.0) & (t < 1.0 + seconds_low)
    measured = pd.DataFrame({"t": t, "pack_voltage_v": np.where(low, 700.0, 800.0)})
    faults = pd.DataFrame({"t": t, "undervoltage_flag": 0})
    return check_flag_consistency(measured, faults, "pack_voltage_v", "undervoltage_flag",
                                  limit=720.0, clear=730.0, direction="below")


def test_consistency_missed_flag() -> None:
    """Below the limit for 2 s with the flag at 0 gives one missed-flag event."""
    events = uv_case(2.0)
    assert [e.check for e in events] == ["missed_flag"]


def test_consistency_short_excursion() -> None:
    """Below the limit for only 0.3 s is within debounce, so no event."""
    assert uv_case(0.3) == []


# check_bms_flags

def test_bms_flags_one_interval() -> None:
    """A flag that is on for one block gives one event; a flag that stays 0 gives none."""
    faults = pd.DataFrame({"t": times(10), "overtemp_flag": [0, 0, 1, 1, 1, 0, 0, 0, 0, 0],
                           "undervoltage_flag": 0})
    events = check_bms_flags(faults)
    assert [(e.signal, e.t_start, e.t_end) for e in events] == [("overtemp_flag", 0.2, 0.4)]
