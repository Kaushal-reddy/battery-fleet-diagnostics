CREATE TABLE cells (
  cell_id        TEXT PRIMARY KEY,
  ambient_temp_c REAL NOT NULL,
  grp            TEXT NOT NULL,
  n_discharge    INTEGER NOT NULL,
  ref_capacity_ah REAL,
  ref_cycle_idx  INTEGER,            -- last cycle of the reference window; earlier cycles are break-in
  included       INTEGER NOT NULL,   -- 1 = in analysis fleet
  exclude_reason TEXT
);

CREATE TABLE discharge_summary (
  cell_id        TEXT NOT NULL REFERENCES cells(cell_id),
  cycle_idx      INTEGER NOT NULL,
  capacity_ah    REAL NOT NULL,
  max_temp_c     REAL,
  mean_voltage_v REAL,
  duration_s     REAL,
  start_time     TEXT,
  PRIMARY KEY (cell_id, cycle_idx)
);

CREATE TABLE impedance (
  cell_id  TEXT NOT NULL REFERENCES cells(cell_id),
  test_idx INTEGER NOT NULL,
  re_ohm   REAL,
  rct_ohm  REAL,
  PRIMARY KEY (cell_id, test_idx)
);

-- Discharge cycles removed during cleaning; raw_cycle_idx is the index before renumbering.
CREATE TABLE dropped_cycles (
  cell_id        TEXT NOT NULL REFERENCES cells(cell_id),
  raw_cycle_idx  INTEGER NOT NULL,
  capacity_ah    REAL,
  cell_median_ah REAL,
  reason         TEXT NOT NULL,
  PRIMARY KEY (cell_id, raw_cycle_idx)
);

CREATE TABLE flags (
  cell_id   TEXT NOT NULL REFERENCES cells(cell_id),
  cycle_idx INTEGER,
  rule      TEXT NOT NULL,
  value     REAL,
  threshold REAL,
  note      TEXT
);
