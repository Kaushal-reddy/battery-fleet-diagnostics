"""Diagnostic checks that look for faults using only the decoded CAN data."""

from dataclasses import asdict, dataclass, fields

import numpy as np
import pandas as pd
from scipy.integrate import cumulative_trapezoid

from src import config
from bms_can import decode_log


@dataclass
class Event:
    check: str
    signal: str
    t_start: float
    t_end: float
    t_detect: float    # earliest time a live monitor running this check could raise the event
    value: float
    note: str


def mask_to_intervals(t, mask, min_duration: float = 0.0) -> list[tuple[float, float]]:
    """Contiguous runs where mask is true, as (first true time, last true time), dropping short runs."""
    t = np.asarray(t, dtype=float)
    m = np.asarray(mask, dtype=bool)
    # Pad with False so every run has a rising edge (+1) and a falling edge (-1).
    edges = np.diff(np.concatenate(([0], m.astype(int), [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1) - 1
    return [(float(t[s]), float(t[e])) for s, e in zip(starts, ends) if t[e] - t[s] >= min_duration]


def align(left: pd.DataFrame, right: pd.DataFrame) -> pd.DataFrame:
    """Attach the latest right-hand value at or before each left-hand time (both sorted by t)."""
    return pd.merge_asof(left, right, on="t", direction="backward")


def extreme(df: pd.DataFrame, signal: str, start: float, end: float, high: bool) -> float:
    """Highest (or lowest) value of a signal inside [start, end]."""
    seg = df.loc[(df["t"] >= start) & (df["t"] <= end), signal]
    return float(seg.max() if high else seg.min())


def check_timeout(df: pd.DataFrame, message: str, period: float) -> list[Event]:
    """One event per gap between frames longer than TIMEOUT_FACTOR periods."""
    limit = config.TIMEOUT_FACTOR * period
    t = df["t"].to_numpy()
    gaps = np.diff(t)
    # Detected once the next frame is overdue: last frame + limit.
    return [Event("timeout", message, float(t[i]), float(t[i + 1]), float(t[i] + limit), float(gaps[i]),
                  f"no {message} for {gaps[i]:.2f} s (limit {limit:.2f} s)")
            for i in np.flatnonzero(gaps > limit)]


def check_range(df: pd.DataFrame, signal: str, lo: float, hi: float) -> list[Event]:
    """One event per contiguous interval where the signal is outside [lo, hi]."""
    v = df[signal]
    events = []
    for start, end in mask_to_intervals(df["t"], (v < lo) | (v > hi)):
        high = extreme(df, signal, start, end, high=True) > hi
        value = extreme(df, signal, start, end, high=high)
        side = f"above {hi}" if high else f"below {lo}"
        events.append(Event("range", signal, start, end, start, value, f"{signal} {side}, reached {value:g}"))
    return events


def check_stuck(status: pd.DataFrame) -> list[Event]:
    """Flag flat soc_pct runs during which the measured current says SoC should have moved."""
    active = status["pack_current_a"].abs() > config.STUCK_MIN_CURRENT_A
    changed = status["soc_pct"].diff().ne(0)  # also True on the first row
    # A run ends when the value changes or the current drops too low to expect a change.
    run_id = (changed | ~active).cumsum()
    events = []
    for _, run in status[active].groupby(run_id[active]):
        if len(run) < 2:
            continue
        t = run["t"].to_numpy()
        # Running integral of |I| dt: A*s -> Ah -> percent of pack capacity.
        ah = cumulative_trapezoid(run["pack_current_a"].abs().to_numpy(), t, initial=0) / 3600
        expected_pct = ah * 100 / config.PACK_CAPACITY_AH
        if expected_pct[-1] < config.STUCK_MIN_EXPECTED_PCT:
            continue
        t_hit = t[np.argmax(expected_pct >= config.STUCK_MIN_EXPECTED_PCT)]
        events.append(Event(
            "stuck", "soc_pct", float(t[0]), float(t[-1]), float(t_hit), float(expected_pct[-1]),
            f"soc_pct flat at {run['soc_pct'].iloc[0]:g} % for {t[-1] - t[0]:.1f} s; current implies "
            f"{expected_pct[-1]:.2f} % change"))
    return events


def check_flag_consistency(measured: pd.DataFrame, faults: pd.DataFrame, signal: str, flag: str,
                           limit: float, clear: float, direction: str) -> list[Event]:
    """Missed flag: past the limit with flag 0. False flag: flag 1 inside the clear threshold."""
    df = align(faults[["t", flag]], measured[["t", signal]])
    v = df[signal]
    past = v > limit if direction == "above" else v < limit
    inside_clear = v < clear if direction == "above" else v > clear
    min_duration = config.DEBOUNCE_S + config.CONSISTENCY_MARGIN_S
    high = direction == "above"
    events = []
    for check, mask, text in [("missed_flag", past & (df[flag] == 0), f"{signal} past {limit} but {flag} = 0"),
                              ("false_flag", inside_clear & (df[flag] == 1), f"{flag} = 1 but {signal} inside {clear}")]:
        for start, end in mask_to_intervals(df["t"], mask, min_duration):
            value = extreme(df, signal, start, end, high)
            events.append(Event(check, flag, start, end, start + min_duration, value,
                                f"{text} for {end - start:.1f} s, reached {value:g}"))
    return events


def check_bms_flags(faults: pd.DataFrame) -> list[Event]:
    """Report each interval where the BMS itself raised a fault flag (its own response, not a defect)."""
    events = []
    for flag in faults.columns.drop("t"):
        for start, end in mask_to_intervals(faults["t"], faults[flag] == 1):
            events.append(Event("bms_flag", flag, start, end, start, end - start,
                                f"BMS set {flag} for {end - start:.1f} s"))
    return events


def run_all_checks(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Run every check on the decoded messages and return one event table sorted by start time."""
    status, temps, faults = dfs["BMS_Status"], dfs["BMS_Temps"], dfs["BMS_Faults"]
    events = []
    for name, period in config.PERIODS.items():
        events += check_timeout(dfs[name], name, period)
    events += check_range(temps, "max_cell_temp_c", config.TEMP_MIN_C, config.TEMP_LIMIT_C)
    events += check_range(status, "pack_voltage_v", config.UV_LIMIT_V, config.OV_LIMIT_V)
    events += check_stuck(status)
    events += check_flag_consistency(temps, faults, "max_cell_temp_c", "overtemp_flag",
                                     config.TEMP_LIMIT_C, config.TEMP_CLEAR_C, "above")
    events += check_flag_consistency(status, faults, "pack_voltage_v", "undervoltage_flag",
                                     config.UV_LIMIT_V, config.UV_CLEAR_V, "below")
    events += check_bms_flags(faults)
    table = pd.DataFrame([asdict(e) for e in events], columns=[f.name for f in fields(Event)])
    return table.sort_values("t_start").reset_index(drop=True)


def main() -> None:
    """Run the checks on the decoded log and save the events."""
    events = run_all_checks(decode_log.load_decoded())
    events.to_csv(config.CAN_OUT / "events.csv", index=False)
    print(f"  SYNTHETIC DATA: {len(events)} events")
    with pd.option_context("display.max_colwidth", None, "display.width", 200):
        print(events.round(2).to_string(index=False))


if __name__ == "__main__":
    main()
