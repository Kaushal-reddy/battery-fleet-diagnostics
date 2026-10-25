"""Run the whole pipeline: fleet analytics on the NASA data, then the synthetic CAN diagnostics."""

import time

from src import analysis, anomalies, build_db, config, ingest, report
from bms_can import checks, decode_log, evaluate, simulate_log

N_STEPS = 8


def status(n: int, name: str, detail: str, t0: float) -> None:
    """Print a one-line status after a pipeline step."""
    print(f"[{n}/{N_STEPS}] {name}: done ({detail}) [{time.perf_counter() - t0:.1f} s]\n")


def main() -> None:
    """Run every step in order, stopping early if the NASA data is missing."""
    if not ingest.list_cell_files(config.RAW_DIR):
        raise SystemExit(f"No B*.mat files in {config.RAW_DIR}. Download the NASA data first (see README).")
    t0 = time.perf_counter()

    # Part A: fleet analytics
    cells, discharge, impedance, drop_log = ingest.load_all(config.RAW_DIR)
    status(1, "ingest", f"{len(cells)} cells, {len(discharge)} discharge cycles kept, {len(drop_log)} dropped", t0)

    build_db.build(cells, discharge, impedance, drop_log)
    status(2, "build_db", config.DB_PATH.relative_to(config.ROOT).as_posix(), t0)

    conn = build_db.connect()
    metrics = analysis.cell_metrics(conn)
    flags = anomalies.run_all(conn)
    status(3, "analysis + anomalies", f"{len(metrics)} fleet cells, {len(flags)} flags", t0)

    report.write_summary(conn)
    conn.close()
    status(4, "report", "reports/summary.md", t0)

    # Part B: CAN diagnostics on a synthetic log
    simulate_log.main()
    status(5, "simulate", "bms_can/output/bms_run.log", t0)

    decode_log.main()
    status(6, "decode", "bms_can/output/decoded_*.csv", t0)

    checks.main()
    status(7, "checks", "bms_can/output/events.csv", t0)

    evaluate.main()
    status(8, "evaluate", "reports/can_results.md", t0)


if __name__ == "__main__":
    main()
