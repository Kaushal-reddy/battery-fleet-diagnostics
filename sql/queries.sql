-- Fleet queries. Each starts with a "-- name:" line so analysis.py can load it by name.
-- :soh_threshold and :eol_capacity_ah are bound from config.py at run time.
-- Fade is measured from cells.ref_capacity_ah, not the first cycle, so break-in
-- cycles (e.g. B0033) don't make a cell look like it gained capacity.
-- Cycles before cells.ref_cycle_idx are break-in: kept in the data, not counted as degradation.

-- name: fleet_overview
WITH last_cycle AS (
  SELECT cell_id, MAX(cycle_idx) AS n_cycles
  FROM discharge_summary
  GROUP BY cell_id
)
SELECT c.grp,
       COUNT(*)                                AS n_cells,
       SUM(l.n_cycles)                         AS total_cycles,
       AVG(c.ref_capacity_ah)                  AS mean_ref_capacity_ah,
       AVG(d.capacity_ah)                      AS mean_last_capacity_ah,
       AVG(d.capacity_ah / c.ref_capacity_ah)  AS mean_last_soh
FROM cells c
JOIN last_cycle l        ON l.cell_id = c.cell_id
JOIN discharge_summary d ON d.cell_id = c.cell_id AND d.cycle_idx = l.n_cycles
WHERE c.included = 1
GROUP BY c.grp
ORDER BY c.grp;

-- name: soh_per_cycle
SELECT d.cell_id,
       c.grp,
       d.cycle_idx,
       d.capacity_ah,
       d.max_temp_c,
       d.capacity_ah / c.ref_capacity_ah AS soh,
       d.capacity_ah - LAG(d.capacity_ah) OVER (
         PARTITION BY d.cell_id ORDER BY d.cycle_idx
       ) AS delta_ah,
       d.cycle_idx < c.ref_cycle_idx AS before_ref
FROM discharge_summary d
JOIN cells c ON c.cell_id = d.cell_id
WHERE c.included = 1
ORDER BY d.cell_id, d.cycle_idx;

-- name: cycles_to_threshold
-- NULL means the threshold was never reached before the test ended.
SELECT c.cell_id,
       c.grp,
       c.ref_capacity_ah,
       MAX(d.cycle_idx) AS n_cycles,
       MIN(CASE WHEN d.capacity_ah / c.ref_capacity_ah < :soh_threshold
                THEN d.cycle_idx END) AS first_cycle_below_soh,
       MIN(CASE WHEN d.capacity_ah < :eol_capacity_ah
                THEN d.cycle_idx END) AS first_cycle_below_eol
FROM cells c
JOIN discharge_summary d ON d.cell_id = c.cell_id
WHERE c.included = 1
  AND d.cycle_idx >= c.ref_cycle_idx
GROUP BY c.cell_id
ORDER BY c.grp, c.cell_id;

-- name: fade_ranking
WITH last_cycle AS (
  SELECT cell_id, MAX(cycle_idx) AS n_cycles
  FROM discharge_summary
  GROUP BY cell_id
)
SELECT c.cell_id,
       c.grp,
       l.n_cycles,
       c.ref_cycle_idx,
       c.ref_capacity_ah,
       d.capacity_ah AS last_capacity_ah,
       (c.ref_capacity_ah - d.capacity_ah)
         / NULLIF(l.n_cycles - c.ref_cycle_idx, 0) * 100 AS fade_ah_per_100
FROM cells c
JOIN last_cycle l        ON l.cell_id = c.cell_id
JOIN discharge_summary d ON d.cell_id = c.cell_id AND d.cycle_idx = l.n_cycles
WHERE c.included = 1
ORDER BY fade_ah_per_100 DESC;

-- name: temp_vs_fade
WITH last_cycle AS (
  SELECT cell_id, MAX(cycle_idx) AS n_cycles
  FROM discharge_summary
  GROUP BY cell_id
)
SELECT c.cell_id,
       c.grp,
       (SELECT AVG(max_temp_c) FROM discharge_summary WHERE cell_id = c.cell_id)
                                           AS mean_max_temp_c,
       c.ref_capacity_ah - d.capacity_ah   AS total_fade_ah
FROM cells c
JOIN last_cycle l        ON l.cell_id = c.cell_id
JOIN discharge_summary d ON d.cell_id = c.cell_id AND d.cycle_idx = l.n_cycles
WHERE c.included = 1
ORDER BY c.grp, c.cell_id;

-- name: excluded_cells
SELECT cell_id, grp, n_discharge, exclude_reason
FROM cells
WHERE included = 0
ORDER BY cell_id;
