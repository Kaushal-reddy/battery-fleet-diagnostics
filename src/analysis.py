"""Run the fleet SQL queries and compute per-cell degradation metrics."""

import re
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from src import config
from src.build_db import connect

QUERIES_PATH = config.SQL_DIR / "queries.sql"

# Bound to the :named placeholders in queries.sql, so thresholds live only in config.py.
SQL_PARAMS = {
    "soh_threshold": config.SOH_THRESHOLD,
    "eol_capacity_ah": config.EOL_CAPACITY_AH,
}


def load_queries(path: Path) -> dict[str, str]:
    """Split a .sql file into {name: sql} on its '-- name:' marker lines."""
    parts = re.split(r"^-- name:\s*(\w+)\s*$", path.read_text(), flags=re.MULTILINE)
    # parts = [header, name1, sql1, name2, sql2, ...]
    return {name: sql.strip() for name, sql in zip(parts[1::2], parts[2::2])}


def run_query(conn: sqlite3.Connection, name: str) -> pd.DataFrame:
    """Run one named query from queries.sql and return the result."""
    return pd.read_sql_query(load_queries(QUERIES_PATH)[name], conn, params=SQL_PARAMS)


def impedance_growth(conn: sqlite3.Connection) -> pd.DataFrame:
    """Median of last N minus median of first N Re and Rct tests per fleet cell."""
    imp = pd.read_sql_query(
        "SELECT i.cell_id, i.test_idx, i.re_ohm, i.rct_ohm FROM impedance i "
        "JOIN cells c ON c.cell_id = i.cell_id WHERE c.included = 1 "
        "ORDER BY i.cell_id, i.test_idx", conn)

    n = config.IMP_EDGE_TESTS

    # Single tests are noisy (e.g. B0055's first Rct is 50 % above its later values),
    # so compare a few tests at each end. NaN if there aren't 2N tests.
    def delta(s: pd.Series) -> float:
        s = s.dropna()
        return float(s.tail(n).median() - s.head(n).median()) if len(s) >= 2 * n else np.nan

    by_cell = imp.groupby("cell_id")
    return pd.DataFrame({
        "delta_re": by_cell["re_ohm"].apply(delta),
        "delta_rct": by_cell["rct_ohm"].apply(delta),
    })


def cell_metrics(conn: sqlite3.Connection) -> pd.DataFrame:
    """One row per fleet cell: fade rate, total fade, resistance growth, mean peak temp."""
    soh = run_query(conn, "soh_per_cycle")
    soh = soh[soh["before_ref"] == 0]  # break-in cycles are not degradation
    # Slope of a straight line through capacity vs cycle; flip the sign so fade is positive.
    fade_rate = soh.groupby("cell_id").apply(
        lambda g: -np.polyfit(g["cycle_idx"], g["capacity_ah"], 1)[0] * 100)

    metrics = run_query(conn, "temp_vs_fade").set_index("cell_id")
    metrics["fade_rate_ah_per_100"] = fade_rate
    metrics = metrics.join(impedance_growth(conn))
    cols = ["grp", "fade_rate_ah_per_100", "total_fade_ah", "delta_re", "delta_rct", "mean_max_temp_c"]
    return metrics[cols].reset_index()


if __name__ == "__main__":
    pd.set_option("display.width", 200)
    with connect() as conn:
        for name in load_queries(QUERIES_PATH):
            df = run_query(conn, name)
            print(f"\n=== {name} ({len(df)} rows) ===")
            print(df.head(30).round(4).to_string(index=False))
        print("\n=== cell_metrics ===")
        print(cell_metrics(conn).round(4).to_string(index=False))
    conn.close()
