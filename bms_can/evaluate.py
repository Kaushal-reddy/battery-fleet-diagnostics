"""Score the CAN checks against the injected faults and write reports/can_results.md."""

import json

import matplotlib

matplotlib.use("Agg")  # write files only, no window
import matplotlib.pyplot as plt
import pandas as pd

from src import config
from src.report import md_table, save
from bms_can import decode_log

# Known side effects of each fault: (fault id, check, signal) -> why the event is expected.
EXPECTED_SECONDARY = {
    ("F2", "bms_flag", "overtemp_flag"):
        f"BMS correctly raised overtemp as F2 heated the pack; it clears only below {config.TEMP_CLEAR_C} °C "
        "(hysteresis), so it stays on while the pack cools after the window",
    ("F4", "range", "pack_voltage_v"):
        "F4's extra voltage sag also takes pack voltage below the range check's lower limit",
}


def fault_signal(fault: dict) -> str:
    """The signal (or, for a dropout, the message) a fault acts on."""
    return fault.get("signal", fault.get("message"))


def overlaps(event: pd.Series, fault: dict) -> bool:
    """True if the event interval overlaps the fault window widened by MATCH_TOLERANCE_S."""
    tol = config.MATCH_TOLERANCE_S
    return event["t_start"] <= fault["end"] + tol and event["t_end"] >= fault["start"] - tol


def match_faults(events: pd.DataFrame, faults: list[dict]) -> tuple[pd.DataFrame, set]:
    """For each fault, find the earliest-detected event of the expected check on the faulted signal."""
    rows, primary = [], set()
    for f in faults:
        hits = events[(events["check"] == f["expected_check"]) & (events["signal"] == fault_signal(f))]
        hits = hits[hits.apply(overlaps, axis=1, fault=f)]
        row = {"fault": f["id"], "type": f["type"], "window_s": f"{f['start']:g}-{f['end']:g}",
               "expected_check": f["expected_check"], "detected": "yes" if len(hits) else "no",
               "t_detect_s": float("nan"), "latency_s": float("nan"), "event": "—"}
        if len(hits):
            idx = hits["t_detect"].idxmin()
            primary.add(idx)
            row.update(t_detect_s=events.at[idx, "t_detect"],
                       latency_s=events.at[idx, "t_detect"] - f["start"], event=events.at[idx, "note"])
        rows.append(row)
    return pd.DataFrame(rows), primary


def classify_others(events: pd.DataFrame, faults: list[dict], primary: set) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split non-primary events into expected secondaries (with a reason) and false positives."""
    secondary, false_pos = [], []
    for idx, e in events.drop(index=list(primary)).iterrows():
        reasons = [(f["id"], EXPECTED_SECONDARY[(f["id"], e["check"], e["signal"])]) for f in faults
                   if overlaps(e, f) and (f["id"], e["check"], e["signal"]) in EXPECTED_SECONDARY]
        base = {"check": e["check"], "signal": e["signal"], "t_start": e["t_start"], "t_end": e["t_end"]}
        if reasons:
            fault_id, reason = reasons[0]
            secondary.append({**base, "fault": fault_id, "reason": reason})
        else:
            false_pos.append({**base, "value": e["value"], "note": e["note"]})
    return pd.DataFrame(secondary), pd.DataFrame(false_pos)


def latency_notes(faults: list[dict], detections: pd.DataFrame, events: pd.DataFrame,
                  status: pd.DataFrame) -> list[str]:
    """One sentence per detected fault on what sets its latency, from config and the decoded data."""
    det = detections.set_index("fault")
    notes = []
    for f in faults:
        if det.at[f["id"], "detected"] != "yes":
            continue
        if f["type"] == "dropout":
            limit = config.TIMEOUT_FACTOR * config.PERIODS[f["message"]]
            why = (f"a frame counts as missing once {limit:.1f} s ({config.TIMEOUT_FACTOR:g} periods) pass "
                   "without one, measured from the last frame before the gap")
        elif f["type"] == "overtemp":
            flag = events[(events["check"] == "bms_flag") & (events["signal"] == "overtemp_flag")]
            why = (f"the fault heats the pack gradually, so the reading takes this long to pass "
                   f"{config.TEMP_LIMIT_C} °C; this is set by the physics, not the check")
            if len(flag):
                why += f" (the BMS's own overtemp flag set at t = {flag['t_start'].min():.1f} s)"
        elif f["type"] == "stuck":
            w = status[(status["t"] >= f["start"]) & (status["t"] < f["end"])]
            amps = w["pack_current_a"].abs().median()
            floor = config.STUCK_MIN_EXPECTED_PCT / (amps * 100 / (3600 * config.PACK_CAPACITY_AH))
            why = (f"at {amps:.0f} A, SoC needs {floor:.0f} s to move {config.STUCK_MIN_EXPECTED_PCT:g} %; a flat "
                   "reading cannot be called stuck sooner without false alarms on healthy flat runs")
        else:  # uv_no_flag
            why = (f"the check waits the {config.DEBOUNCE_S:g} s debounce plus a {config.CONSISTENCY_MARGIN_S:g} s "
                   "margin, so the BMS gets a fair chance to raise the flag first")
        notes.append(f"**{f['id']}** ({det.at[f['id'], 'latency_s']:.1f} s): {why}.")
    return notes


def break_gaps(df: pd.DataFrame, period: float) -> pd.DataFrame:
    """Insert a blank row inside each timeout-length gap so plotted lines break instead of bridging it."""
    gap = df["t"].diff() > config.TIMEOUT_FACTOR * period
    blanks = pd.DataFrame({"t": (df["t"] - df["t"].diff() / 2)[gap]})
    return pd.concat([df, blanks]).sort_values("t")


def plot_timeline(dfs: dict[str, pd.DataFrame], truth: pd.DataFrame, faults: list[dict],
                  detections: pd.DataFrame) -> str:
    """Stacked voltage, temperature, SoC and flag traces with fault windows and detection times."""
    status = break_gaps(dfs["BMS_Status"], config.PERIODS["BMS_Status"])
    temps, flags = dfs["BMS_Temps"], dfs["BMS_Faults"]
    fig, axes = plt.subplots(5, 1, figsize=(10, 11), sharex=True,
                             gridspec_kw={"height_ratios": [2, 2, 2, 1, 1]})
    ax_v, ax_t, ax_s, ax_ot, ax_uv = axes

    ax_v.plot(status["t"], status["pack_voltage_v"], lw=1, label="pack voltage")
    ax_v.axhline(config.UV_LIMIT_V, color="red", ls="--", lw=1, label=f"UV limit {config.UV_LIMIT_V:g} V")
    ax_v.set_ylabel("Voltage (V)")

    ax_t.plot(temps["t"], temps["max_cell_temp_c"], lw=1, label="max cell temp")
    ax_t.axhline(config.TEMP_LIMIT_C, color="red", ls="--", lw=1, label=f"limit {config.TEMP_LIMIT_C:g} °C")
    ax_t.set_ylabel("Temperature (°C)")

    ax_s.plot(status["t"], status["soc_pct"], lw=1, label="reported soc_pct (CAN)")
    ax_s.plot(truth["t"], truth["soc"] * 100, color="gray", ls="--", lw=1, label="true SoC (simulator truth)")
    ax_s.set_ylabel("SoC (%)")

    for ax, flag in [(ax_ot, "overtemp_flag"), (ax_uv, "undervoltage_flag")]:
        ax.step(flags["t"], flags[flag], where="post", lw=1, label=flag)
        ax.set_ylim(-0.15, 1.15)
        ax.set_yticks([0, 1])
        ax.set_ylabel("Flag")

    # The panel where each fault shows, so its detection time is marked there.
    panel = {"BMS_Status": ax_v, "max_cell_temp_c": ax_t, "soc_pct": ax_s, "undervoltage_flag": ax_uv}
    det = detections.set_index("fault")
    for f in faults:
        for ax in axes:
            ax.axvspan(f["start"], f["end"], color="orange", alpha=0.15, lw=0)
        ax_v.annotate(f["id"], ((f["start"] + f["end"]) / 2, 1.02), xycoords=("data", "axes fraction"),
                      ha="center", va="bottom", fontsize=9)
        if det.at[f["id"], "detected"] == "yes":
            panel[fault_signal(f)].axvline(det.at[f["id"], "t_detect_s"], color="crimson", lw=1.5, ls=":",
                                           label="detection time")

    # Legend corners picked to sit where each trace leaves empty space.
    for ax, loc in zip(axes, ["center left", "upper right", "lower left", "center left", "center left"]):
        handles, labels = ax.get_legend_handles_labels()
        unique = dict(zip(labels, handles))
        ax.legend(unique.values(), unique.keys(), loc=loc, fontsize=8)
    axes[-1].set_xlabel("Time (s)")
    fig.suptitle("SYNTHETIC DATA: simulated BMS CAN log. Shaded: injected fault windows")
    return save(fig, "can_timeline.png")


def write_report(stats: pd.DataFrame, detections: pd.DataFrame, secondary: pd.DataFrame,
                 false_pos: pd.DataFrame, events: pd.DataFrame, notes: list[str], fig: str,
                 ground_truth: dict) -> None:
    """Write reports/can_results.md from the computed tables."""
    n_det = (detections["detected"] == "yes").sum()
    fp_text = md_table(false_pos, digits=2) if len(false_pos) else "None."
    sec_text = md_table(secondary, digits=1) if len(secondary) else "None."
    md = f"""# CAN log diagnostics

> **Synthetic data.** Every frame in this report comes from a simulated pack and BMS
> (`bms_can/simulate_log.py`, seed {ground_truth['seed']}), not from a real vehicle or energy storage system.

## Summary

- Faults detected: {n_det} of {len(detections)}.
- Expected secondary events: {len(secondary)}. False positives: {len(false_pos)}.
- Detection time is the earliest moment a monitor running the check live could raise the event;
  latency = detection time − fault start.

## Message timing

{md_table(stats, digits=4)}

## Fault detection

A fault counts as detected if an event from its expected check, on the faulted signal, overlaps the fault
window widened by ±{config.MATCH_TOLERANCE_S:g} s.

{md_table(detections, digits=1)}

### What sets each latency

{chr(10).join(f"- {n}" for n in notes)}

## Other events

### Expected secondary

{sec_text}

### False positives

{fp_text}

<details><summary>All events</summary>

{md_table(events, digits=2)}

</details>

## Timeline

![CAN timeline]({fig})
"""
    (config.REPORT_DIR / "can_results.md").write_text(md, encoding="utf-8")


def main() -> None:
    """Evaluate the saved events against the ground truth and write the report and figure."""
    dfs = decode_log.load_decoded()
    events = pd.read_csv(config.CAN_OUT / "events.csv")
    ground_truth = json.loads((config.CAN_OUT / "ground_truth.json").read_text())
    faults = ground_truth["faults"]
    truth = pd.read_csv(config.CAN_OUT / "truth.csv")  # plotted for comparison only, never used to score

    detections, primary = match_faults(events, faults)
    secondary, false_pos = classify_others(events, faults, primary)
    notes = latency_notes(faults, detections, events, dfs["BMS_Status"])
    fig = plot_timeline(dfs, truth, faults, detections)
    write_report(decode_log.message_stats(dfs), detections, secondary, false_pos, events, notes, fig,
                 ground_truth)

    print("  SYNTHETIC DATA")
    print(detections.drop(columns="event").round(1).to_string(index=False))
    print(f"  expected secondary: {len(secondary)}, false positives: {len(false_pos)}")
    for _, e in false_pos.iterrows():
        print(f"  FALSE POSITIVE: {e.to_dict()}")
    print("  wrote reports/can_results.md and figures/can_timeline.png")


if __name__ == "__main__":
    main()
