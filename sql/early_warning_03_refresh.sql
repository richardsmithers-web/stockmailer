-- Early warning panel, part 3 of 3: the daily snapshot and the Power BI views.
--
-- sp_refresh_early_warning() copies vw_early_warning into the table ew_daily, so the heavy maths runs once a
-- day (the stockmailer calls it at 07:00, right after it has fetched the latest FRED and ONS figures).
-- Power BI and the Saturday email both read ew_daily, never the view directly.

CREATE OR REPLACE PROCEDURE `project-e042f011-a587-4cbe-8f7.Market_Data_Project.sp_refresh_early_warning`()
BEGIN
  CREATE OR REPLACE TABLE `project-e042f011-a587-4cbe-8f7.Market_Data_Project.ew_daily`
  PARTITION BY DATE_TRUNC(warning_date, YEAR)
  CLUSTER BY indicator_id
  OPTIONS (description = 'Snapshot of vw_early_warning, refreshed by sp_refresh_early_warning() at 07:00. refreshed_at = when.')
  AS SELECT *, CURRENT_TIMESTAMP() AS refreshed_at FROM `project-e042f011-a587-4cbe-8f7.Market_Data_Project.vw_early_warning`;
END;

-- Power BI: every day, every indicator, with its rules alongside (for tooltips and the "why" panel)
CREATE OR REPLACE VIEW `project-e042f011-a587-4cbe-8f7.Market_Data_Project.rpt_fact_early_warning`
OPTIONS (description = 'Power BI: early warning readings per day per indicator (from ew_daily) with lights; light_score 0 green, 1 amber, 2 red.')
AS
SELECT w.warning_date, w.indicator_id, w.sort, w.area, w.name, w.unit, w.value, w.value_change, w.pctile_20y,
       w.data_as_of, w.light,
       CASE w.light WHEN 'RED' THEN 2 WHEN 'AMBER' THEN 1 WHEN 'GREEN' THEN 0 END AS light_score,
       w.detail, w.refreshed_at
FROM `project-e042f011-a587-4cbe-8f7.Market_Data_Project.ew_daily` w;

CREATE OR REPLACE VIEW `project-e042f011-a587-4cbe-8f7.Market_Data_Project.rpt_dim_early_warning_indicator`
OPTIONS (description = 'Power BI: one row per early warning indicator with its rules in plain English, score weight and maximum light points (from ew_indicators).')
AS
SELECT indicator_id, sort, area, name, unit, what_it_measures, amber_rule, red_rule, why_it_matters, source_note,
       score_weight, IF(red_level IS NULL, 1, 3) AS max_light_points
FROM `project-e042f011-a587-4cbe-8f7.Market_Data_Project.ew_indicators`;

-- Daily summary: how many lights are red / amber each day, and the 1-10 risk score.
-- The score rule (identical to the Power BI measure EW Score):
--   points     = sum over indicators of weight x (amber 1, red 3, green 0)
--   max points = sum over indicators WITH DATA of weight x (3, or 1 for the Buffett indicator, which is never red)
--   score      = 1 + 9 x (points / half of max points), capped at 10
-- So 10 means at least half of the panel, by weight, is red (or the equivalent in ambers).
CREATE OR REPLACE VIEW `project-e042f011-a587-4cbe-8f7.Market_Data_Project.rpt_fact_early_warning_daily`
OPTIONS (description = 'Power BI: per day, counts of red / amber / green / no-data lights and the 1-10 risk score (same rule as the DAX measure EW Score: amber 1 point, red 3, times score_weight; 10 = half the maximum points of the indicators with data).')
AS
SELECT w.warning_date,
       COUNTIF(w.light = 'RED') AS n_red, COUNTIF(w.light = 'AMBER') AS n_amber,
       COUNTIF(w.light = 'GREEN') AS n_green, COUNTIF(w.light = 'NO DATA') AS n_no_data,
       SUM(i.score_weight * CASE w.light WHEN 'RED' THEN 3 WHEN 'AMBER' THEN 1 ELSE 0 END) AS score_points,
       SUM(IF(w.light <> 'NO DATA', i.score_weight * IF(i.red_level IS NULL, 1, 3), 0)) AS score_max_points,
       ROUND(1 + 9 * LEAST(1, SAFE_DIVIDE(
         SUM(i.score_weight * CASE w.light WHEN 'RED' THEN 3 WHEN 'AMBER' THEN 1 ELSE 0 END),
         0.5 * SUM(IF(w.light <> 'NO DATA', i.score_weight * IF(i.red_level IS NULL, 1, 3), 0)))), 1) AS risk_score
FROM `project-e042f011-a587-4cbe-8f7.Market_Data_Project.ew_daily` w
JOIN `project-e042f011-a587-4cbe-8f7.Market_Data_Project.ew_indicators` i USING (indicator_id)
GROUP BY w.warning_date;
