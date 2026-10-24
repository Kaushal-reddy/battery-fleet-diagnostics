"""Simulate a battery pack and its BMS, and write a synthetic CAN log with injected faults."""

import json
from dataclasses import asdict, dataclass

import can
import cantools
import numpy as np
import pandas as pd

from src import config

FAULTS = [
    {"id": "F1", "type": "dropout",    "message": "BMS_Status",
     "start": 120.0, "end": 123.0, "expected_check": "timeout"},
    {"id": "F2", "type": "overtemp",   "signal": "max_cell_temp_c",
     "start": 200.0, "end": 260.0, "expected_check": "range"},
    # 60 s, not 30 s: at -150 A true SoC must move >= STUCK_MIN_EXPECTED_PCT while soc_pct is frozen
    {"id": "F3", "type": "stuck",      "signal": "soc_pct",
     "start": 330.0, "end": 390.0, "expected_check": "stuck"},
    {"id": "F4", "type": "uv_no_flag", "signal": "undervoltage_flag",
     "start": 450.0, "end": 480.0, "expected_check": "missed_flag"},
]

DEBOUNCE_STEPS = round(config.DEBOUNCE_S / config.SIM_DT_S)


@dataclass
class PackState:
    t: float
    soc: float
    current_a: float
    voltage_v: float
    max_temp_c: float
    min_temp_c: float
    overtemp_flag: int
    uv_flag: int       # the firmware's own decision, before the F4 bug masks it
    ot_timer: int      # steps the overtemp condition has held so far
    uv_timer: int      # steps the undervoltage condition has held so far


def fault_active(faults: list[dict], fault_id: str, t: float) -> bool:
    """True if the given fault is scheduled and t is inside its window."""
    return any(f["id"] == fault_id and f["start"] <= t < f["end"] for f in faults)


def load_current(t: float) -> float:
    """Pack current at time t: base discharge with high-current steps."""
    for start, end in config.LOAD_STEPS_S:
        if start <= t < end:
            return config.LOAD_STEP_A
    return config.LOAD_BASE_A


def ocv(soc: float) -> float:
    """Open-circuit voltage, linear between empty and full."""
    return config.PACK_V_EMPTY + soc * (config.PACK_V_FULL - config.PACK_V_EMPTY)


def update_flag(flag: int, timer: int, active: bool, cleared: bool) -> tuple[int, int]:
    """Set after the condition holds for DEBOUNCE_S; clear once past the clear threshold."""
    if flag:
        return (0, 0) if cleared else (1, 0)
    timer = timer + 1 if active else 0
    return (1, 0) if timer >= DEBOUNCE_STEPS else (0, timer)


def initial_state() -> PackState:
    """Pack at rest at the start of the run."""
    return PackState(t=0.0, soc=config.INITIAL_SOC, current_a=0.0, voltage_v=ocv(config.INITIAL_SOC),
                     max_temp_c=config.AMBIENT_C, min_temp_c=config.AMBIENT_C - config.CELL_TEMP_SPREAD_C,
                     overtemp_flag=0, uv_flag=0, ot_timer=0, uv_timer=0)


def step(s: PackState, t: float, faults: list[dict]) -> PackState:
    """Advance the pack physics and firmware flags by one time step."""
    dt = config.SIM_DT_S
    current = load_current(t)
    soc = s.soc - (-current) * dt / (3600 * config.PACK_CAPACITY_AH)

    voltage = ocv(soc) + current * config.PACK_R_OHM  # current < 0, so this is a sag
    if fault_active(faults, "F4", t):
        voltage -= config.F4_EXTRA_SAG_V

    heating = current**2 * config.PACK_R_OHM * config.PACK_HEAT_GAIN_C_PER_J
    if fault_active(faults, "F2", t):
        heating += config.F2_EXTRA_HEAT_C_PER_S
    cooling = config.PACK_COOLING_PER_S * (s.max_temp_c - config.AMBIENT_C)
    temp = s.max_temp_c + (heating - cooling) * dt

    ot_flag, ot_timer = update_flag(s.overtemp_flag, s.ot_timer,
                                    temp > config.TEMP_LIMIT_C, temp < config.TEMP_CLEAR_C)
    uv_flag, uv_timer = update_flag(s.uv_flag, s.uv_timer,
                                    voltage < config.UV_LIMIT_V, voltage > config.UV_CLEAR_V)
    return PackState(t=t, soc=soc, current_a=current, voltage_v=voltage,
                     max_temp_c=temp, min_temp_c=temp - config.CELL_TEMP_SPREAD_C,
                     overtemp_flag=ot_flag, uv_flag=uv_flag, ot_timer=ot_timer, uv_timer=uv_timer)


def message_values(s: PackState, soc_pct: float, uv_masked: bool) -> dict[str, dict]:
    """Signal values the BMS would put in each message for this state."""
    return {
        "BMS_Status": {"pack_voltage_v": s.voltage_v, "pack_current_a": s.current_a, "soc_pct": soc_pct},
        "BMS_Temps": {"max_cell_temp_c": s.max_temp_c, "min_cell_temp_c": s.min_temp_c},
        "BMS_Faults": {"overtemp_flag": s.overtemp_flag,
                       "undervoltage_flag": 0 if uv_masked else s.uv_flag,
                       "comm_fault_flag": 0},
    }


def simulate(faults: list[dict], db: cantools.database.Database) -> tuple[pd.DataFrame, list[can.Message]]:
    """Run the pack for SIM_DURATION_S and return the true states and the CAN frames sent."""
    rng = np.random.default_rng(config.SIM_SEED)
    n_steps = round(config.SIM_DURATION_S / config.SIM_DT_S)
    # Time is counted in whole steps so periods and debounce never suffer float drift.
    period_steps = {name: round(p / config.SIM_DT_S) for name, p in config.PERIODS.items()}
    next_send = {name: 1 for name in config.PERIODS}

    s = initial_state()
    truth, frames = [], []
    frozen_soc_pct = None
    for k in range(1, n_steps + 1):
        t = round(k * config.SIM_DT_S, 6)  # avoid float noise like 0.30000000000000004
        s = step(s, t, faults)
        truth.append(asdict(s))

        if fault_active(faults, "F3", t):
            if frozen_soc_pct is None:
                frozen_soc_pct = s.soc * 100  # value at the window start
            soc_pct = frozen_soc_pct
        else:
            soc_pct = s.soc * 100
        values = message_values(s, soc_pct, uv_masked=fault_active(faults, "F4", t))

        for name, signals in values.items():
            if k < next_send[name]:
                continue
            next_send[name] += period_steps[name]
            if name == "BMS_Status" and fault_active(faults, "F1", t):
                continue  # frame suppressed; the schedule keeps running
            frames.append(can.Message(
                timestamp=t + rng.uniform(-config.TIMESTAMP_JITTER_S, config.TIMESTAMP_JITTER_S),
                arbitration_id=db.get_message_by_name(name).frame_id,
                data=db.encode_message(name, signals),
                is_extended_id=False,
            ))

    frames.sort(key=lambda m: m.timestamp)  # jitter can swap frames sent in the same step
    return pd.DataFrame(truth), frames


def write_outputs(truth: pd.DataFrame, frames: list[can.Message]) -> None:
    """Write the CAN log, the ground-truth fault file and the true-value trace."""
    config.CAN_OUT.mkdir(exist_ok=True)
    with can.CanutilsLogWriter(config.CAN_OUT / "bms_run.log") as writer:
        for msg in frames:
            writer.on_message_received(msg)
    truth.to_csv(config.CAN_OUT / "truth.csv", index=False)
    ground_truth = {
        "note": "SYNTHETIC DATA: simulated pack and BMS, not a real vehicle or Megapack log",
        "seed": config.SIM_SEED,
        "faults": FAULTS,
        "periods_s": config.PERIODS,
        "limits": {
            "temp_limit_c": config.TEMP_LIMIT_C, "temp_clear_c": config.TEMP_CLEAR_C,
            "uv_limit_v": config.UV_LIMIT_V, "uv_clear_v": config.UV_CLEAR_V,
            "debounce_s": config.DEBOUNCE_S,
        },
    }
    (config.CAN_OUT / "ground_truth.json").write_text(json.dumps(ground_truth, indent=2))


def print_margins(normal: pd.DataFrame, faulted: pd.DataFrame) -> None:
    """Show that normal running stays inside the limits and F3 is detectable."""
    print(f"  fault-free run: min voltage {normal.voltage_v.min():.1f} V (UV limit {config.UV_LIMIT_V}), "
          f"max temp {normal.max_temp_c.max():.1f} C (limit {config.TEMP_LIMIT_C})")
    for f in FAULTS:
        w = faulted[(faulted.t >= f["start"]) & (faulted.t < f["end"])]
        print(f"  {f['id']} {f['start']:.0f}-{f['end']:.0f} s: voltage {w.voltage_v.min():.1f}-"
              f"{w.voltage_v.max():.1f} V, max temp up to {w.max_temp_c.max():.1f} C, "
              f"true SoC change {(w.soc.iloc[0] - w.soc.iloc[-1]) * 100:.2f} %")


def main() -> None:
    """Simulate the faulted run, write its outputs, and check the fault-free margins."""
    db = cantools.database.load_file(config.CAN_DIR / "bms.dbc")
    truth, frames = simulate(FAULTS, db)
    write_outputs(truth, frames)
    print(f"  SYNTHETIC DATA: wrote {len(frames)} frames to {config.CAN_OUT / 'bms_run.log'}")
    normal, _ = simulate([], db)
    print_margins(normal, truth)


if __name__ == "__main__":
    main()
