"""Create data/fleet.db from the cleaned ingest tables."""

import sqlite3

import pandas as pd

from src import config, ingest

# Parent table first: every other table references cells(cell_id).
TABLE_ORDER = ["cells", "discharge_summary", "impedance", "dropped_cycles"]


def connect() -> sqlite3.Connection:
    """Open the fleet database with foreign key enforcement on."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite leaves this off by default
    return conn


def build(cells: pd.DataFrame, discharge: pd.DataFrame, impedance: pd.DataFrame,
          drop_log: pd.DataFrame) -> None:
    """Rebuild the database from scratch and print row counts per table."""
    config.DB_PATH.unlink(missing_ok=True)
    tables = dict(zip(TABLE_ORDER, [cells, discharge, impedance, drop_log]))

    with connect() as conn:
        conn.executescript((config.SQL_DIR / "schema.sql").read_text())
        for name in TABLE_ORDER:
            # append, not replace: replace would drop our schema and its keys
            tables[name].to_sql(name, conn, if_exists="append", index=False)

        for name in TABLE_ORDER + ["flags"]:
            n = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            print(f"  {name}: {n} rows")
    conn.close()


if __name__ == "__main__":
    build(*ingest.load_all(config.RAW_DIR))
