"""Paths, thresholds and limits shared by every module."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "fleet.db"
SQL_DIR = ROOT / "sql"
FIG_DIR = ROOT / "reports" / "figures"
REPORT_DIR = ROOT / "reports"
CAN_DIR = ROOT / "bms_can"
CAN_OUT = CAN_DIR / "output"

# Part A
RATED_CAPACITY_AH = 2.0
REF_CYCLES = 5               # cycles in the rolling median used for reference capacity
REF_SEARCH_CYCLES = 20       # reference = highest rolling median within this many early cycles
LOW_CAP_FRACTION = 0.5       # drop glitch cycles below this x cell median
EOL_CAPACITY_AH = 1.4
SOH_THRESHOLD = 0.80
MAX_VALID_CAPACITY_AH = 1.1 * RATED_CAPACITY_AH  # highest real fleet value is ~2.04 Ah
FADE_MAD_K = 3.0
MAD_SCALE = 1.4826
MIN_GROUP_SIZE = 4
CAP_JUMP_AH = 0.03
CAP_JUMP_LARGE_AH = 0.10     # rises above this follow a partial/glitch cycle, not just rest
THERMAL_WINDOW = 10          # cycles
THERMAL_MARGIN_C = 3.0
RES_MAD_K = 3.0
IMP_EDGE_TESTS = 3           # resistance growth = median of last N tests - median of first N

# Part A: cell groups (discharge ambient + load), to be confirmed against READMEs in Stage 1
CELL_GROUPS = {
    **{c: "24C_2A" for c in ["B0005", "B0006", "B0007", "B0018", "B0036"]},
    **{c: "24C_4A" for c in ["B0033", "B0034"]},
    **{c: "43C_4A" for c in ["B0029", "B0030", "B0031", "B0032"]},
    **{c: "4C_1A" for c in ["B0045", "B0046", "B0047", "B0048"]},
    **{c: "4C_2A" for c in ["B0049", "B0050", "B0051", "B0052",
                            "B0053", "B0054", "B0055", "B0056"]},
    **{c: "24C_sq4A" for c in ["B0025", "B0026", "B0027", "B0028"]},
    **{c: "mixed" for c in ["B0038", "B0039", "B0040",
                            "B0041", "B0042", "B0043", "B0044"]},
}

_SQ_REASON = "4 A square-wave load profile, only ~28 cycles"
_MIXED_REASON = "load current and ambient temperature change between cycles"
EXCLUDED_CELLS = {
    **{c: _SQ_REASON for c in ["B0025", "B0026", "B0027", "B0028"]},
    **{c: _MIXED_REASON for c in ["B0038", "B0039", "B0040",
                                  "B0042", "B0043", "B0044"]},
    "B0041": "load current changes between cycles (4 A / 1 A)",
    "B0050": "only 11 valid discharge cycles out of 25; values erratic",
    "B0052": "only 4 valid discharge cycles out of 25",
}

# Part B: pack simulation
SIM_SEED = 42
SIM_DURATION_S = 600.0
SIM_DT_S = 0.1
PACK_CAPACITY_AH = 200.0
PACK_R_OHM = 0.05
PACK_V_EMPTY = 700.0
PACK_V_FULL = 900.0
AMBIENT_C = 20.0
INITIAL_SOC = 0.90
TIMESTAMP_JITTER_S = 0.002
CELL_TEMP_SPREAD_C = 3.0     # min cell temp = max cell temp - this

# Part B: load profile (discharge current is negative)
LOAD_BASE_A = -150.0
LOAD_STEP_A = -250.0
LOAD_STEPS_S = [(30.0, 90.0), (280.0, 320.0), (520.0, 580.0)]  # kept clear of the fault windows

# Part B: thermal model, dT/dt = gain * I^2 * R - cooling * (T - ambient)
PACK_HEAT_GAIN_C_PER_J = 3.6e-5  # steady-state rise ~4 C at -150 A
PACK_COOLING_PER_S = 0.01        # 100 s thermal time constant

# Part B: fault strengths
F2_EXTRA_HEAT_C_PER_S = 1.0  # drives max_cell_temp_c past TEMP_LIMIT_C inside the F2 window
F4_EXTRA_SAG_V = 140.0       # drives pack voltage from ~845 V to below UV_LIMIT_V

# Part B: message periods (s)
PERIODS = {"BMS_Status": 0.1, "BMS_Temps": 0.5, "BMS_Faults": 0.1}

# Part B: safety limits and check settings
TEMP_LIMIT_C = 55.0
TEMP_CLEAR_C = 52.0          # hysteresis
TEMP_MIN_C = 0.0             # lower bound for the temperature range check
UV_LIMIT_V = 720.0
UV_CLEAR_V = 730.0           # hysteresis
OV_LIMIT_V = 910.0           # upper bound for the voltage range check
DEBOUNCE_S = 0.5
CONSISTENCY_MARGIN_S = 0.3
TIMEOUT_FACTOR = 3.0
STUCK_MIN_CURRENT_A = 20.0
STUCK_MIN_EXPECTED_PCT = 1.0  # flag a flat soc_pct run if current says SoC should have moved this much
MATCH_TOLERANCE_S = 1.0
