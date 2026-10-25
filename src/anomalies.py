"""Rule-based anomaly flags for the fleet; every rule returns rows shaped like the flags table."""

import sqlite3

import numpy as np
import pandas as pd

from src import config
from src.analysis import cell_metrics, run_query
from src.build_db import connect

FLAG_COLUMNS = ["cell_id", "cycle_idx", "rule", "value", "threshold", "note"]


def robust_threshold(values, k: float) -> float:
    """Return median + k * MAD_SCALE * MAD, or NaN if MAD is 0."""
    x = pd.Series(values).dropna().to_numpy()
    med = np.median(x)
    mad = np.median(np.abs(x - med))
    # Median and MAD barely move when one cell is extreme, unlike mean and std.
    return float(med + k * config.MAD_SCALE * mad) if mad > 0 else float("nan")


def fade_thresholds(metrics: pd.DataFrame) -> dict[str, tuple[float, str]]:
    """Return {grp: (fade-rate threshold, note)}; small groups use the fleet-wide threshold."""
    fleet = robust_threshold(metrics["fade_rate_ah_per_100"], config.FADE_MAD_K)
    out = {}
    for grp, g in metrics.groupby("grp"):
        if len(g) < config.MIN_GROUP_SIZE:
            out[grp] = (fleet, f"{grp} has {len(g)} cells (< {config.MIN_GROUP_SIZE}); "
                               "fleet-wide threshold used")
        else:
            out[grp] = (robust_threshold(g["fade_rate_ah_per_100"], config.FADE_MAD_K),
                        f"threshold from {grp} ({len(g)} cells)")
    return out


def flag_fast_degraders(metrics: pd.DataFrame) -> pd.DataFrame:
    """Flag cells whose fade rate is above their group's robust threshold."""
    rows = []
    for grp, (thr, note) in fade_thresholds(metrics).items():
        if np.isnan(thr):
            print(f"  fast_degrader: skipped {grp}, threshold is NaN (MAD = 0)")
            continue
        g = metrics[(metrics["grp"] == grp) & (metrics["fade_rate_ah_per_100"] > thr)]
        rows += [{"cell_id": r.cell_id, "cycle_idx": None, "rule": "fast_degrader",
                  "value": r.fade_rate_ah_per_100, "threshold": thr, "note": note}
                 for r in g.itertuples()]
    return pd.DataFrame(rows, columns=FLAG_COLUMNS)


def jump_note(rest_h: float, prev_low: bool, delta_ah: float) -> str:
    """Explain a capacity rise from the evidence: a long rest, a low previous cycle, or neither."""
    if rest_h > config.REST_GAP_H and prev_low:
        return f"after {rest_h:.0f} h rest and a low (partial) previous cycle"
    if rest_h > config.REST_GAP_H:
        return f"after {rest_h:.0f} h rest; capacity recovery, real behaviour"
    if prev_low:
        return "previous cycle low (partial discharge); return to trend"
    if delta_ah > config.CAP_JUMP_LARGE_AH:
        return "large rise with no long rest or low previous cycle; cause not identified"
    return "small rise at normal cycle spacing; normal variation"


def flag_capacity_jumps(discharge: pd.DataFrame) -> pd.DataFrame:
    """Flag cycles where capacity rose by more than CAP_JUMP_AH since the previous cycle."""
    d = discharge.sort_values(["cell_id", "cycle_idx"]).copy()
    by_cell = d.groupby("cell_id")
    start = pd.to_datetime(d["start_time"], format="ISO8601")  # some timestamps lack microseconds
    d["rest_h"] = start.groupby(d["cell_id"]).diff().dt.total_seconds() / 3600
    # Previous cycle compared with the median of the cycles just before it.
    recent = by_cell["capacity_ah"].transform(
        lambda s: s.shift(2).rolling(config.JUMP_BASELINE_CYCLES).median())
    d["prev_low"] = by_cell["capacity_ah"].shift(1) < config.PARTIAL_CYCLE_FRACTION * recent

    # delta_ah comes from the LAG() window in soh_per_cycle; break-in cycles are skipped.
    d = d[(d["before_ref"] == 0) & (d["delta_ah"] > config.CAP_JUMP_AH)]
    note = [jump_note(r.rest_h, r.prev_low, r.delta_ah) for r in d.itertuples()]
    return pd.DataFrame({
        "cell_id": d["cell_id"], "cycle_idx": d["cycle_idx"], "rule": "capacity_jump",
        "value": d["delta_ah"], "threshold": config.CAP_JUMP_AH, "note": note,
    }, columns=FLAG_COLUMNS)


def flag_thermal_outliers(discharge: pd.DataFrame) -> pd.DataFrame:
    """Flag cycles whose peak temperature exceeds the trailing median by THERMAL_MARGIN_C."""
    d = discharge.sort_values(["cell_id", "cycle_idx"]).copy()
    # shift(1) so a hot cycle cannot raise its own baseline.
    d["baseline"] = d.groupby("cell_id")["max_temp_c"].transform(
        lambda s: s.shift(1).rolling(config.THERMAL_WINDOW).median())
    d = d[d["baseline"].notna() & (d["max_temp_c"] > d["baseline"] + config.THERMAL_MARGIN_C)]
    return pd.DataFrame({
        "cell_id": d["cell_id"], "cycle_idx": d["cycle_idx"], "rule": "thermal_outlier",
        "value": d["max_temp_c"], "threshold": d["baseline"] + config.THERMAL_MARGIN_C,
        "note": f"peak temp > median of previous {config.THERMAL_WINDOW} cycles + "
                f"{config.THERMAL_MARGIN_C:g} C",
    }, columns=FLAG_COLUMNS)


def flag_resistance_growth(metrics: pd.DataFrame) -> pd.DataFrame:
    """Flag cells whose Re or Rct growth is above the fleet-wide robust threshold."""
    rows = []
    for col, name in [("delta_re", "Re"), ("delta_rct", "Rct")]:
        thr = robust_threshold(metrics[col], config.RES_MAD_K)
        if np.isnan(thr):
            continue
        rows += [{"cell_id": r.cell_id, "cycle_idx": None, "rule": f"resistance_growth_{name.lower()}",
                  "value": getattr(r, col), "threshold": thr,
                  "note": f"{name} growth above fleet-wide threshold"}
                 for r in metrics[metrics[col] > thr].itertuples()]
    return pd.DataFrame(rows, columns=FLAG_COLUMNS)


def run_all(conn: sqlite3.Connection) -> pd.DataFrame:
    """Run every rule, replace the contents of the flags table, and return the flags."""
    metrics = cell_metrics(conn)
    discharge = run_query(conn, "soh_per_cycle")
    parts = [flag_fast_degraders(metrics), flag_capacity_jumps(discharge),
             flag_thermal_outliers(discharge), flag_resistance_growth(metrics)]
    flags = pd.concat([p for p in parts if len(p)], ignore_index=True)
    flags["cycle_idx"] = flags["cycle_idx"].astype("Int64")  # nullable: cell-level flags have none

    conn.execute("DELETE FROM flags")
    flags.to_sql("flags", conn, if_exists="append", index=False)
    conn.commit()
    return flags


if __name__ == "__main__":
    pd.set_option("display.width", 200)
    with connect() as conn:
        flags = run_all(conn)
    conn.close()
    print("Flags per rule:")
    print(flags.groupby("rule").size().to_string())
    for rule in ["fast_degrader", "resistance_growth_re", "resistance_growth_rct", "thermal_outlier"]:
        print(f"\n{rule}:")
        print(flags[flags.rule == rule].round(4).to_string(index=False))
    jumps = flags[flags.rule == "capacity_jump"]
    print("\ncapacity_jump per cell:", jumps.groupby("cell_id").size().to_dict())
    print("largest capacity jumps:")
    print(jumps.nlargest(8, "value").round(4).to_string(index=False))
