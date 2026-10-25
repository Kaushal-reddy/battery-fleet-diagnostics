# CAN log diagnostics

> **Synthetic data.** Every frame in this report comes from a simulated pack and BMS
> (`bms_can/simulate_log.py`, seed 42), not from a real vehicle or energy storage system.

## Summary

- Faults detected: 4 of 4.
- Expected secondary events: 2. False positives: 0.
- Detection time is the earliest moment a monitor running the check live could raise the event;
  latency = detection time − fault start.

## Message timing

| message | count | expected_period_s | mean_period_s | median_period_s | max_gap_s | std_period_s |
|---|---|---|---|---|---|---|
| BMS_Status | 5970 | 0.1000 | 0.1005 | 0.1000 | 3.1005 | 0.0389 |
| BMS_Temps | 1200 | 0.5000 | 0.5000 | 0.5000 | 0.5039 | 0.0017 |
| BMS_Faults | 6000 | 0.1000 | 0.1000 | 0.1000 | 0.1040 | 0.0016 |

## Fault detection

A fault counts as detected if an event from its expected check, on the faulted signal, overlaps the fault
window widened by ±1 s.

| fault | type | window_s | expected_check | detected | t_detect_s | latency_s | event |
|---|---|---|---|---|---|---|---|
| F1 | dropout | 120-123 | timeout | yes | 120.2 | 0.2 | no BMS_Status for 3.10 s (limit 0.30 s) |
| F2 | overtemp | 200-260 | range | yes | 237.6 | 37.6 | max_cell_temp_c above 55.0, reached 69 |
| F3 | stuck | 330-390 | stuck | yes | 377.4 | 47.4 | soc_pct flat at 81.5 % for 60.5 s; current implies 1.26 % change |
| F4 | uv_no_flag | 450-480 | missed_flag | yes | 450.8 | 0.8 | pack_voltage_v past 720.0 but undervoltage_flag = 0 for 29.9 s, reached 709.7 |

### What sets each latency

- **F1** (0.2 s): a frame counts as missing once 0.3 s (3 periods) pass without one, measured from the last frame before the gap.
- **F2** (37.6 s): the fault heats the pack gradually, so the reading takes this long to pass 55.0 °C; this is set by the physics, not the check (the BMS's own overtemp flag set at t = 236.8 s).
- **F3** (47.4 s): at 150 A, SoC needs 48 s to move 1 %; a flat reading cannot be called stuck sooner without false alarms on healthy flat runs.
- **F4** (0.8 s): the check waits the 0.5 s debounce plus a 0.3 s margin, so the BMS gets a fair chance to raise the flag first.

## Other events

### Expected secondary

| check | signal | t_start | t_end | fault | reason |
|---|---|---|---|---|---|
| bms_flag | overtemp_flag | 236.8 | 316.7 | F2 | BMS correctly raised overtemp as F2 heated the pack; it clears only below 52.0 °C (hysteresis), so it stays on while the pack cools after the window |
| range | pack_voltage_v | 450.0 | 479.9 | F4 | F4's extra voltage sag also takes pack voltage below the range check's lower limit |

### False positives

None.

<details><summary>All events</summary>

| check | signal | t_start | t_end | t_detect | value | note |
|---|---|---|---|---|---|---|
| timeout | BMS_Status | 119.90 | 123.00 | 120.20 | 3.10 | no BMS_Status for 3.10 s (limit 0.30 s) |
| bms_flag | overtemp_flag | 236.80 | 316.70 | 236.80 | 79.90 | BMS set overtemp_flag for 79.9 s |
| range | max_cell_temp_c | 237.60 | 301.10 | 237.60 | 69.00 | max_cell_temp_c above 55.0, reached 69 |
| stuck | soc_pct | 329.40 | 389.90 | 377.40 | 1.26 | soc_pct flat at 81.5 % for 60.5 s; current implies 1.26 % change |
| range | pack_voltage_v | 450.00 | 479.90 | 450.00 | 709.70 | pack_voltage_v below 720.0, reached 709.7 |
| missed_flag | undervoltage_flag | 450.00 | 479.90 | 450.80 | 709.70 | pack_voltage_v past 720.0 but undervoltage_flag = 0 for 29.9 s, reached 709.7 |

</details>

## Timeline

![CAN timeline](figures/can_timeline.png)
