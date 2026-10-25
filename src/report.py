"""Build the Part A figures and reports/summary.md from the fleet database."""

import sqlite3

import matplotlib

matplotlib.use("Agg")  # write files only, no window
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src import config
from src.analysis import cell_metrics, run_query
from src.anomalies import fade_thresholds
from src.build_db import connect

DPI = 150
GROUP_ORDER = ["24C_2A", "24C_4A", "43C_4A", "4C_1A", "4C_2A"]
GROUP_COLORS = dict(zip(GROUP_ORDER, plt.get_cmap("tab10").colors))


def md_table(df: pd.DataFrame, digits: int = 3) -> str:
    """Render a DataFrame as a Markdown table, floats rounded, missing values as an em dash."""
    def fmt(v) -> str:
        if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA:
            return "—"
        if isinstance(v, (float, np.floating)):
            return f"{v:.{digits}f}"
        return str(v)
    if "cycle_idx" in df.columns:
        # NaN for cell-level flags makes this column float; nullable Int64 keeps whole numbers
        df = df.assign(cycle_idx=df["cycle_idx"].astype("Int64"))
    lines =["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(fmt(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def group_legend(ax: plt.Axes, groups, extra=()) -> None:
    """Add a legend with one coloured entry per group, plus any extra handles."""
    handles = [plt.Line2D([], [], color=GROUP_COLORS[g], lw=3, label=g) for g in groups]
    ax.legend(handles=handles + list(extra), fontsize=8)


def plot_soh(soh: pd.DataFrame, fast: set[str]) -> str:
    """SoH vs cycle per cell, coloured by group; fast degraders thick and labelled."""
    soh = soh[soh["before_ref"] == 0]
    fig, ax = plt.subplots(figsize=(10, 6))
    for cell_id, g in soh.groupby("cell_id"):
        thick = cell_id in fast
        ax.plot(g["cycle_idx"], g["soh"], color=GROUP_COLORS[g["grp"].iloc[0]],
                lw=2.5 if thick else 0.9, alpha=1 if thick else 0.7, zorder=3 if thick else 2)
        if thick:
            ax.annotate(cell_id, (g["cycle_idx"].iloc[-1], g["soh"].iloc[-1]),
                        xytext=(4, 0), textcoords="offset points", fontsize=8, va="center")
    threshold = ax.axhline(config.SOH_THRESHOLD, color="k", ls="--", lw=1,
                           label=f"{config.SOH_THRESHOLD:.0%} SoH threshold")
    ax.set(xlabel="Discharge cycle (break-in cycles omitted)", ylabel="SoH (capacity / reference)",
           title="State of health vs cycle")
    group_legend(ax, sorted(soh["grp"].unique(), key=GROUP_ORDER.index), [threshold])
    return save(fig, "soh_vs_cycle.png")


def plot_fade(metrics: pd.DataFrame, thresholds: dict) -> str:
    """Fade rate per cell, sorted within group, with each group's threshold line."""
    m = metrics.assign(order=metrics["grp"].map(GROUP_ORDER.index))
    m = m.sort_values(["order", "fade_rate_ah_per_100"]).reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.bar(m.index, m["fade_rate_ah_per_100"], color=m["grp"].map(GROUP_COLORS))
    for grp, g in m.groupby("grp"):
        ax.hlines(thresholds[grp][0], g.index.min() - 0.4, g.index.max() + 0.4, colors="k", ls="--")
    ax.set_xticks(m.index, m["cell_id"], rotation=60, fontsize=8)
    ax.set(ylabel="Fade rate (Ah per 100 cycles)", title="Fade rate by cell and group")
    line = plt.Line2D([], [], color="k", ls="--", label="group threshold")
    group_legend(ax, GROUP_ORDER, [line])
    return save(fig, "fade_rate_by_cell.png")


def plot_temp_vs_fade(metrics: pd.DataFrame, fast: set[str]) -> str:
    """Scatter of mean peak temperature against fade rate, coloured by group."""
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(metrics["mean_max_temp_c"], metrics["fade_rate_ah_per_100"],
               c=metrics["grp"].map(GROUP_COLORS), s=40)
    for r in metrics[metrics["cell_id"].isin(fast)].itertuples():
        ax.annotate(r.cell_id, (r.mean_max_temp_c, r.fade_rate_ah_per_100),
                    xytext=(5, 0), textcoords="offset points", fontsize=8, va="center")
    ax.set(xlabel="Mean peak cell temperature per discharge (°C)",
           ylabel="Fade rate (Ah per 100 cycles)", title="Temperature vs fade rate")
    group_legend(ax, GROUP_ORDER)
    return save(fig, "temp_vs_fade.png")


def plot_resistance(metrics: pd.DataFrame, flags: pd.DataFrame) -> str:
    """Scatter of Re growth against Rct growth (mOhm), with fleet thresholds."""
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(metrics["delta_re"] * 1000, metrics["delta_rct"] * 1000,
               c=metrics["grp"].map(GROUP_COLORS), s=40)
    for rule, draw in [("resistance_growth_re", ax.axvline), ("resistance_growth_rct", ax.axhline)]:
        thr = flags.loc[flags["rule"] == rule, "threshold"]
        if len(thr):
            draw(thr.iloc[0] * 1000, color="k", ls="--", lw=1)
    flagged = flags.loc[flags["rule"].str.startswith("resistance"), "cell_id"].unique()
    for r in metrics[metrics["cell_id"].isin(flagged)].itertuples():
        ax.annotate(r.cell_id, (r.delta_re * 1000, r.delta_rct * 1000),
                    xytext=(5, 0), textcoords="offset points", fontsize=8, va="center")
    ax.axhline(0, color="grey", lw=0.5)
    ax.set(xlabel="Re growth (mΩ)", ylabel="Rct growth (mΩ)",
           title=f"Resistance growth (median of last {config.IMP_EDGE_TESTS} tests - first "
                 f"{config.IMP_EDGE_TESTS}); dashed = fleet threshold")
    group_legend(ax, GROUP_ORDER)
    return save(fig, "resistance_growth.png")


def save(fig: plt.Figure, name: str) -> str:
    """Save a figure into FIG_DIR and return its path relative to the reports folder."""
    config.FIG_DIR.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(config.FIG_DIR / name, dpi=DPI)
    plt.close(fig)
    return f"figures/{name}"


def threshold_summary(thr: pd.DataFrame) -> pd.DataFrame:
    """Per group: how many cells reached 80 % SoH and EOL, and the median cycle when they did."""
    eligible = thr["ref_capacity_ah"] > config.EOL_CAPACITY_AH
    rows = []
    for grp, g in thr.groupby("grp"):
        e = g[eligible[g.index]]
        rows.append({
            "grp": grp, "cells": len(g),
            "reached_soh": f"{g['first_cycle_below_soh'].notna().sum()}/{len(g)}",
            "median_cycle_soh": g["first_cycle_below_soh"].median(),
            "reached_eol": f"{e['first_cycle_below_eol'].notna().sum()}/{len(e)}" if len(e) else "n/a",
            "median_cycle_eol": e["first_cycle_below_eol"].median() if len(e) else np.nan,
        })
    return pd.DataFrame(rows)


def threshold_per_cell(thr: pd.DataFrame) -> pd.DataFrame:
    """Per-cell crossing cycles as whole numbers, with EOL marked n/a where the reference starts below it."""
    eligible = thr["ref_capacity_ah"] > config.EOL_CAPACITY_AH
    eol = thr["first_cycle_below_eol"].astype("Int64").astype(object).where(eligible, "n/a")
    return thr.assign(first_cycle_below_soh=thr["first_cycle_below_soh"].astype("Int64"),
                      first_cycle_below_eol=eol)


def findings(metrics: pd.DataFrame, flags: pd.DataFrame, soh: pd.DataFrame,
             cells: pd.DataFrame, thresholds: dict) -> list[str]:
    """Plain-language findings, built only from computed values."""
    out = []
    med = metrics.groupby("grp")["fade_rate_ah_per_100"].median().sort_values()
    out.append(f"Median fade rate is highest in {med.index[-1]} ({med.iloc[-1]:.2f} Ah per 100 cycles) "
               f"and lowest in {med.index[0]} ({med.iloc[0]:.2f}).")

    fast = flags[flags["rule"] == "fast_degrader"]
    if len(fast):
        parts = []
        for r in fast.itertuples():
            g = soh[(soh["cell_id"] == r.cell_id) & (soh["before_ref"] == 0)]
            ref = cells.loc[r.cell_id, "ref_capacity_ah"]
            drop = -g["delta_ah"].min() / ref
            parts.append(f"{r.cell_id} ({r.value:.2f} Ah per 100 cycles, threshold {r.threshold:.2f}; "
                         f"largest single-cycle drop {drop:.0%} of reference)")
        out.append("Flagged as fast degraders: " + "; ".join(parts) + ".")
    else:
        out.append("No cell exceeded its group's fade-rate threshold.")

    rho = metrics["mean_max_temp_c"].corr(metrics["fade_rate_ah_per_100"], method="spearman")
    strength = "weak" if abs(rho) < 0.3 else "moderate" if abs(rho) < 0.6 else "strong"
    out.append(f"Across the fleet, mean peak temperature and fade rate have a {strength} rank "
               f"correlation (Spearman ρ = {rho:.2f}); group conditions differ in more than temperature.")

    fleet = cells[cells["included"] == 1]
    rel = fleet["ref_capacity_ah"] / fleet.groupby("grp")["ref_capacity_ah"].transform("median")
    low = rel.idxmin()
    if low not in set(fast["cell_id"]):
        out.append(f"{low} has the lowest reference capacity relative to its group "
                   f"({rel[low]:.0%} of the {fleet.loc[low, 'grp']} median) but a normal fade rate, "
                   "so no rule flags it.")
    return out


def write_summary(conn: sqlite3.Connection) -> None:
    """Generate all figures and write reports/summary.md."""
    cells = pd.read_sql("SELECT * FROM cells", conn).set_index("cell_id")
    drops = pd.read_sql("SELECT d.*, c.included FROM dropped_cycles d JOIN cells c USING (cell_id)", conn)
    flags = pd.read_sql("SELECT * FROM flags", conn)
    metrics = cell_metrics(conn)
    soh = run_query(conn, "soh_per_cycle")
    thr = run_query(conn, "cycles_to_threshold")
    thresholds = fade_thresholds(metrics)
    fast = set(flags.loc[flags["rule"] == "fast_degrader", "cell_id"])

    figs = [("State of health vs cycle", plot_soh(soh, fast)),
            ("Fade rate by cell", plot_fade(metrics, thresholds)),
            ("Temperature vs fade rate", plot_temp_vs_fade(metrics, fast))]
    if metrics["delta_re"].notna().mean() >= 0.5:
        figs.append(("Resistance growth", plot_resistance(metrics, flags)))

    fleet = cells[cells["included"] == 1]
    fleet_drops = drops[drops["included"] == 1]
    drop_counts = (fleet_drops.groupby(["cell_id", "reason"]).size()
                   .rename("cycles_dropped").reset_index())
    per_rule = flags.groupby("rule").agg(flags=("cell_id", "size"), cells=("cell_id", "nunique")).reset_index()
    jumps = flags[flags["rule"] == "capacity_jump"]
    thermal = flags[flags["rule"] == "thermal_outlier"]
    on_jump = thermal.merge(jumps[["cell_id", "cycle_idx"]], on=["cell_id", "cycle_idx"]).shape[0]
    flag_cols = ["cell_id", "cycle_idx", "value", "threshold", "note"]

    eol_note = (f"End of life (EOL) is capacity below {config.EOL_CAPACITY_AH} Ah, counted only for cells whose "
                f"reference is above {config.EOL_CAPACITY_AH} Ah; "
                f"{(fleet['ref_capacity_ah'] <= config.EOL_CAPACITY_AH).sum()} fleet cells start below it.")

    md = f"""# Battery fleet summary

Generated from `data/fleet.db`. Data: NASA PCoE Li-ion battery aging dataset (18650 cells, 2.0 Ah rated).

## Findings

{chr(10).join(f"- {s}" for s in findings(metrics, flags, soh, cells, thresholds))}

## Fleet

- Cells in dataset: {len(cells)}; in analysis fleet: {len(fleet)}; excluded: {len(cells) - len(fleet)}.
- Discharge cycles in the fleet after cleaning: {int(fleet['n_discharge'].sum())}.
- Discharge cycles dropped during cleaning: {len(drops)} in total, {len(fleet_drops)} from fleet cells.

### Group overview

{md_table(run_query(conn, "fleet_overview"))}

### Excluded cells

{md_table(run_query(conn, "excluded_cells"))}

### Cleaning (drop log, fleet cells)

Full log, including excluded cells, is in the `dropped_cycles` table.

{md_table(drop_counts)}

## Cycles to threshold

SoH = capacity / reference capacity, where the reference is the highest {config.REF_CYCLES}-cycle rolling median
in the first {config.REF_SEARCH_CYCLES} cycles. Cycles before the reference point are treated as break-in and not
counted. {eol_note} A dash means the threshold was not reached before the test ended.

{md_table(threshold_summary(thr), digits=0)}

<details><summary>Per cell</summary>

{md_table(threshold_per_cell(thr), digits=3)}

</details>

## Anomaly flags

{md_table(per_rule)}

### Fast degraders

Robust threshold per group: median + {config.FADE_MAD_K:g} × {config.MAD_SCALE} × MAD of fade rate.

{md_table(flags.loc[flags["rule"] == "fast_degrader", flag_cols])}

### Resistance growth

Fleet-wide robust threshold on growth in Re and Rct (Ω).

{md_table(flags.loc[flags["rule"].str.startswith("resistance"), ["rule"] + flag_cols], digits=4)}

### Thermal outliers

{on_jump} of {len(thermal)} thermal outliers fall on a cycle that is also flagged as a capacity jump.

{md_table(thermal[flag_cols], digits=2)}

### Capacity jumps

{len(jumps)} cycles; {(jumps["value"] > config.CAP_JUMP_LARGE_AH).sum()} exceed {config.CAP_JUMP_LARGE_AH} Ah.
Largest 10:

{md_table(jumps.nlargest(10, "value")[flag_cols])}

## Figures

{chr(10).join(f"### {title}{chr(10)}{chr(10)}![{title}]({path}){chr(10)}" for title, path in figs)}
"""
    (config.REPORT_DIR / "summary.md").write_text(md, encoding="utf-8")
    print(f"  wrote reports/summary.md and {len(figs)} figures")


if __name__ == "__main__":
    with connect() as conn:
        write_summary(conn)
    conn.close()
