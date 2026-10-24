"""Load the NASA PCoE .mat files into clean cell, discharge and impedance tables."""

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.io import loadmat

from src import config


def list_cell_files(raw_dir: Path) -> list[tuple[str, Path]]:
    """Return (cell_id, path) for every B*.mat file, sorted by cell id."""
    return [(p.stem, p) for p in sorted(raw_dir.glob("B*.mat"))]


def load_cycles(path: Path) -> np.ndarray:
    """Return the cycle array from one cell's .mat file."""
    mat = loadmat(path, squeeze_me=True, struct_as_record=False)
    return np.atleast_1d(mat[path.stem].cycle)


def as_1d(x) -> np.ndarray:
    """Return x as a 1-D array (squeeze_me turns 1-element arrays into scalars)."""
    return np.atleast_1d(np.asarray(x)).ravel()


def first_value(x) -> float:
    """Return the first element of x as a real float, or NaN if x is empty."""
    arr = as_1d(x)
    return float(np.real(arr[0])) if arr.size else float("nan")


def datevec_to_iso(vec) -> str:
    """Convert a MATLAB date vector [Y, M, D, h, m, s] to an ISO string."""
    y, mo, d, h, mi, s = as_1d(vec).astype(float)
    start = datetime(int(y), int(mo), int(d), int(h), int(mi))
    return (start + timedelta(seconds=s)).isoformat()


def parse_cell(cell_id: str, cycles: np.ndarray) -> tuple[dict, list[dict], list[dict]]:
    """Return the cell row, raw discharge rows and impedance rows for one cell."""
    discharge, impedance, ambients = [], [], []
    for c in cycles:
        if c.type == "discharge":
            ambients.append(float(c.ambient_temperature))
            t = as_1d(c.data.Time)
            discharge.append({
                "cell_id": cell_id,
                "cycle_idx": len(discharge) + 1,
                "capacity_ah": first_value(c.data.Capacity),
                "max_temp_c": float(as_1d(c.data.Temperature_measured).max()),
                "mean_voltage_v": float(as_1d(c.data.Voltage_measured).mean()),
                "duration_s": float(t[-1] - t[0]),
                "start_time": datevec_to_iso(c.time),
            })
        elif c.type == "impedance":
            impedance.append({
                "cell_id": cell_id,
                "test_idx": len(impedance) + 1,
                "re_ohm": first_value(c.data.Re),
                "rct_ohm": first_value(c.data.Rct),
            })

    cell = {
        "cell_id": cell_id,
        "ambient_temp_c": Counter(ambients).most_common(1)[0][0],
        "grp": config.CELL_GROUPS[cell_id],
        "n_discharge": len(discharge),   # overwritten after cleaning
        "ref_capacity_ah": None,         # set after cleaning
        "ref_cycle_idx": None,           # set after cleaning
        "included": int(cell_id not in config.EXCLUDED_CELLS),
        "exclude_reason": config.EXCLUDED_CELLS.get(cell_id),
    }
    return cell, discharge, impedance


def clean_discharge(discharge: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Drop invalid and glitch cycles, renumber cycle_idx; return (kept, drop_log)."""
    cap = discharge["capacity_ah"]
    invalid = cap.isna() | (cap <= 0) | (cap > config.MAX_VALID_CAPACITY_AH)

    # Median per cell over valid cycles only, so empty or impossible values don't skew it.
    median = cap.where(~invalid).groupby(discharge["cell_id"]).transform("median")
    glitch = ~invalid & (cap < config.LOW_CAP_FRACTION * median)

    reason = pd.Series(None, index=discharge.index, dtype=object)
    reason[invalid] = f"missing, <= 0 or > {config.MAX_VALID_CAPACITY_AH:.1f} Ah"
    reason[glitch] = f"< {config.LOW_CAP_FRACTION} x cell median"

    drop_log = discharge.loc[reason.notna(), ["cell_id", "cycle_idx", "capacity_ah"]].copy()
    drop_log = drop_log.rename(columns={"cycle_idx": "raw_cycle_idx"})
    drop_log["cell_median_ah"] = median[reason.notna()]
    drop_log["reason"] = reason[reason.notna()]

    kept = discharge[reason.isna()].copy()
    kept["cycle_idx"] = kept.groupby("cell_id").cumcount() + 1
    return kept.reset_index(drop=True), drop_log.reset_index(drop=True)


def reference_point(caps: pd.Series) -> tuple[float, int]:
    """Return (reference capacity, cycle_idx where its window ends) for one cell."""
    # Capacity is highest early in life, so a low start is break-in, not the true baseline.
    # Reference = highest REF_CYCLES rolling median within the first REF_SEARCH_CYCLES cycles.
    early = caps.head(config.REF_SEARCH_CYCLES).reset_index(drop=True)
    if len(early) < config.REF_CYCLES:
        return float(early.median()), len(early)
    rolling = early.rolling(config.REF_CYCLES).median()
    return float(rolling.max()), int(rolling.idxmax()) + 1  # position -> 1-based cycle_idx


def load_all(raw_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Parse and clean every cell; return (cells, discharge, impedance, drop_log)."""
    cells, discharge, impedance = [], [], []
    for cell_id, path in list_cell_files(raw_dir):
        cell, dis, imp = parse_cell(cell_id, load_cycles(path))
        cells.append(cell)
        discharge.extend(dis)
        impedance.extend(imp)

    cells = pd.DataFrame(cells)
    discharge, drop_log = clean_discharge(pd.DataFrame(discharge))
    impedance = pd.DataFrame(impedance)

    by_cell = discharge.groupby("cell_id")
    cells["n_discharge"] = cells["cell_id"].map(by_cell.size()).fillna(0).astype(int)
    points = by_cell["capacity_ah"].apply(reference_point)
    cells["ref_capacity_ah"] = cells["cell_id"].map(points.map(lambda p: p[0]))
    cells["ref_cycle_idx"] = cells["cell_id"].map(points.map(lambda p: p[1]))

    print(f"Dropped {len(drop_log)} discharge cycles:")
    summary = drop_log.groupby(["cell_id", "reason"]).size()
    print(summary.to_string() if len(summary) else "  none")
    return cells, discharge, impedance, drop_log


if __name__ == "__main__":
    cells, discharge, impedance, drop_log = load_all(config.RAW_DIR)
    print("\nDropped cycles in detail:")
    print(drop_log.to_string(index=False))
    print("\nCells:")
    print(cells.drop(columns="exclude_reason").to_string(index=False))
    print(f"\nRows: cells={len(cells)}, discharge={len(discharge)}, impedance={len(impedance)}")
