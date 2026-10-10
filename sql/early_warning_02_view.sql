-- Early warning panel, part 2 of 3: the view that works out every indicator for every trading day since 2005.
--
-- Output: one row per trading day per indicator (12 indicators), with
--   value          the reading in the indicator's unit (see ew_indicators.unit)
--   value_change   the same reading's change over about one week (5 trading days)
--   light          GREEN / AMBER / RED / NO DATA
--   pctile_20y     where the reading sits among all its readings since 2005 (0 = lowest, 100 = highest)
--   data_as_of     the date of the underlying figure (monthly and quarterly data lag the calendar)
--   detail         one plain-English sentence built from the numbers
--
-- No look-ahead: monthly, weekly and quarterly figures only count from roughly the day they are published
-- (US jobs: month end + 7 days; UK jobs: period end + about 6 weeks; jobless claims: week end + 5 days;
-- Buffett indicator: quarter end + 75 days). So a past row shows what you could have known on that day.
-- One exception: pctile_20y ranks each reading against the WHOLE history, including later years, so the
-- Buffett indicator's amber light (which uses that percentile) has some hindsight in rows before 2026.
--
-- Thresholds come from ew_indicators (part 1). Inputs: hist_macro_20y (FRED, ONS, Bank of England),
-- hist_index_20y (index prices and volumes), fact_daily_prices / hist_prices_20y / hist_prices_20y_us
-- (stock prices), tickers (index membership).

CREATE OR REPLACE VIEW `project-e042f011-a587-4cbe-8f7.Market_Data_Project.vw_early_warning`
OPTIONS (description = 'Early warning panel: 12 indicators x every trading day since 2005, with traffic lights from ew_indicators. Snapshot in ew_daily (sp_refresh_early_warning).')
AS
WITH
cal AS (
  SELECT DISTINCT price_date AS d
  FROM `project-e042f011-a587-4cbe-8f7.Market_Data_Project.hist_index_20y`
  WHERE symbol IN ('^GSPC', '^FTSE') AND price_date >= DATE '2005-01-03'
),
idx AS (
  SELECT symbol, price_date AS d, close, volume
  FROM `project-e042f011-a587-4cbe-8f7.Market_Data_Project.hist_index_20y`
  WHERE symbol IN ('^GSPC', '^FTSE') AND close > 0
),
spx AS (SELECT d, close FROM idx WHERE symbol = '^GSPC'),
mac AS (
  SELECT series, obs_date, value
  FROM `project-e042f011-a587-4cbe-8f7.Market_Data_Project.hist_macro_20y`
  WHERE value IS NOT NULL
),

-- ---------------------------------------------------------------- 1. every input as (series, available-from date, value)
uk_unemp AS (
  SELECT obs_date, value,
         value - MIN(value) OVER (ORDER BY obs_date ROWS BETWEEN 12 PRECEDING AND 1 PRECEDING) AS gap
  FROM mac WHERE series = 'UK_UNEMPLOYMENT_RATE'
),
claims AS (
  SELECT obs_date, ma4,
         MIN(ma4) OVER (ORDER BY obs_date ROWS BETWEEN 51 PRECEDING AND CURRENT ROW) AS low52
  FROM (SELECT obs_date, AVG(value) OVER (ORDER BY obs_date ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) AS ma4
        FROM mac WHERE series = 'ICSA')
),
buffett_q AS (
  SELECT e.obs_date,
         DATE_SUB(DATE_ADD(e.obs_date, INTERVAL 3 MONTH), INTERVAL 1 DAY) AS q_end,
         e.value / 1000 / g.value * 100 AS ratio
  FROM mac e JOIN mac g ON g.series = 'GDP' AND g.obs_date = e.obs_date
  WHERE e.series = 'NCBEILQ027S'
),
buffett_q2 AS (   -- attach the S&P 500 level at each quarter end, for rolling the ratio forward daily
  SELECT b.obs_date, b.q_end, b.ratio, s.close AS spx_q_end
  FROM buffett_q b JOIN spx s ON s.d <= b.q_end AND s.d > DATE_SUB(b.q_end, INTERVAL 10 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY b.q_end ORDER BY s.d DESC) = 1
),
events AS (
  SELECT series, obs_date AS avail, obs_date, value, CAST(NULL AS FLOAT64) AS aux
  FROM mac WHERE series IN ('T10Y3M', 'BAMLH0A0HYM2', 'VIXCLS', 'IUDMNPY', 'IUDBEDR')
  UNION ALL
  SELECT 'SAHM', DATE_ADD(DATE_ADD(obs_date, INTERVAL 1 MONTH), INTERVAL 6 DAY), obs_date, value, NULL
  FROM mac WHERE series = 'SAHMREALTIME'
  UNION ALL
  SELECT 'UK_GAP', DATE_ADD(DATE_ADD(obs_date, INTERVAL 2 MONTH), INTERVAL 15 DAY), obs_date, gap, value
  FROM uk_unemp WHERE gap IS NOT NULL
  UNION ALL
  SELECT 'CLAIMS', DATE_ADD(obs_date, INTERVAL 5 DAY), obs_date, (ma4 / low52 - 1) * 100, ma4
  FROM claims WHERE low52 > 0
  UNION ALL
  SELECT 'BUFFETT', DATE_ADD(q_end, INTERVAL 75 DAY), q_end, ratio, spx_q_end
  FROM buffett_q2 WHERE spx_q_end IS NOT NULL
),

-- ---------------------------------------------------------------- 2. carry each input forward to every trading day
grid AS (
  SELECT c.d, s AS series FROM cal c
  CROSS JOIN UNNEST(['T10Y3M', 'BAMLH0A0HYM2', 'VIXCLS', 'IUDMNPY', 'IUDBEDR', 'SAHM', 'UK_GAP', 'CLAIMS', 'BUFFETT']) AS s
  UNION DISTINCT
  SELECT avail, series FROM events
),
locf AS (
  SELECT g.d, g.series,
         LAST_VALUE(e.value IGNORE NULLS) OVER w AS value,
         LAST_VALUE(e.obs_date IGNORE NULLS) OVER w AS obs_date,
         LAST_VALUE(e.aux IGNORE NULLS) OVER w AS aux
  FROM grid g
  LEFT JOIN events e ON e.series = g.series AND e.avail = g.d
  WINDOW w AS (PARTITION BY g.series ORDER BY g.d ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
),
wide AS (
  SELECT c.d,
    MAX(IF(l.series = 'T10Y3M', l.value, NULL)) AS us_curve,          MAX(IF(l.series = 'T10Y3M', l.obs_date, NULL)) AS us_curve_dt,
    MAX(IF(l.series = 'IUDMNPY', l.value, NULL)) AS gilt10,
    MAX(IF(l.series = 'IUDBEDR', l.value, NULL)) AS bank_rate,         MAX(IF(l.series = 'IUDMNPY', l.obs_date, NULL)) AS gilt_dt,
    MAX(IF(l.series = 'BAMLH0A0HYM2', l.value, NULL)) AS hy,           MAX(IF(l.series = 'BAMLH0A0HYM2', l.obs_date, NULL)) AS hy_dt,
    MAX(IF(l.series = 'VIXCLS', l.value, NULL)) AS vix,                MAX(IF(l.series = 'VIXCLS', l.obs_date, NULL)) AS vix_dt,
    MAX(IF(l.series = 'SAHM', l.value, NULL)) AS sahm,                 MAX(IF(l.series = 'SAHM', l.obs_date, NULL)) AS sahm_dt,
    MAX(IF(l.series = 'UK_GAP', l.value, NULL)) AS uk_gap,
    MAX(IF(l.series = 'UK_GAP', l.aux, NULL)) AS uk_unemp,             MAX(IF(l.series = 'UK_GAP', l.obs_date, NULL)) AS uk_dt,
    MAX(IF(l.series = 'CLAIMS', l.value, NULL)) AS claims_rise,
    MAX(IF(l.series = 'CLAIMS', l.aux, NULL)) AS claims_ma4,           MAX(IF(l.series = 'CLAIMS', l.obs_date, NULL)) AS claims_dt,
    MAX(IF(l.series = 'BUFFETT', l.value, NULL)) AS buffett_q,
    MAX(IF(l.series = 'BUFFETT', l.aux, NULL)) AS buffett_spx_q,       MAX(IF(l.series = 'BUFFETT', l.obs_date, NULL)) AS buffett_dt
  FROM cal c JOIN locf l ON l.d = c.d
  GROUP BY c.d
),

-- ---------------------------------------------------------------- 3. rates, credit, valuation on the daily grid
daily AS (
  SELECT w.*, gilt10 - bank_rate AS uk_curve,
         s.close AS spx_close,
         buffett_q * SAFE_DIVIDE(s.close, buffett_spx_q) AS buffett,
         hy - LAG(hy, 63) OVER (ORDER BY w.d) AS hy_chg3m
  FROM wide w
  LEFT JOIN spx s ON s.d = w.d
),
curves AS (
  SELECT d,
    -- US: count inverted days in the last ~2 years and whether any were in the last ~6 months
    SUM(IF(us_curve < 0, 1, 0)) OVER (ORDER BY d ROWS BETWEEN 503 PRECEDING AND CURRENT ROW) AS us_inv_days_2y,
    MAX(IF(us_curve < 0, 1, 0)) OVER (ORDER BY d ROWS BETWEEN 125 PRECEDING AND CURRENT ROW) AS us_inv_6m,
    SUM(IF(uk_curve < 0, 1, 0)) OVER (ORDER BY d ROWS BETWEEN 503 PRECEDING AND CURRENT ROW) AS uk_inv_days_2y,
    MAX(IF(uk_curve < 0, 1, 0)) OVER (ORDER BY d ROWS BETWEEN 125 PRECEDING AND CURRENT ROW) AS uk_inv_6m
  FROM daily
),

-- ---------------------------------------------------------------- 4. distribution days (index falls on rising volume)
dist AS (
  -- a day's volume only counts if it is at least half its 20-day average: the latest day is sometimes
  -- loaded before the close with only part of the day's volume
  SELECT symbol, d,
         SUM(IF(ret <= -0.002 AND vol_ok AND prev_vol_ok AND volume > prev_volume, 1, 0))
           OVER (PARTITION BY symbol ORDER BY d ROWS BETWEEN 24 PRECEDING AND CURRENT ROW) AS n25
  FROM (SELECT symbol, d, ret, volume, vol_ok,
               LAG(volume) OVER (PARTITION BY symbol ORDER BY d) AS prev_volume,
               LAG(vol_ok) OVER (PARTITION BY symbol ORDER BY d) AS prev_vol_ok
        FROM (SELECT symbol, d, volume,
                     close / LAG(close) OVER (PARTITION BY symbol ORDER BY d) - 1 AS ret,
                     IFNULL(volume >= 0.5 * AVG(volume) OVER (PARTITION BY symbol ORDER BY d ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING), FALSE) AS vol_ok
              FROM idx))
),

-- ---------------------------------------------------------------- 5. US breadth: average S&P 500 stock vs the index
us_ret AS (   -- daily returns worked out within each source, then fact_daily_prices preferred where both exist
  SELECT ticker, d, ret FROM (
    SELECT ticker, d, ret, pref FROM (
      SELECT ticker, price_date AS d, 2 AS pref,
             close / LAG(close) OVER (PARTITION BY ticker ORDER BY price_date) - 1 AS ret
      FROM `project-e042f011-a587-4cbe-8f7.Market_Data_Project.hist_prices_20y_us`
      WHERE index_name = 'S&P 500' AND close > 0
      UNION ALL
      SELECT p.ticker, p.price_date, 1,
             CAST(p.close_price AS FLOAT64) / LAG(CAST(p.close_price AS FLOAT64)) OVER (PARTITION BY p.ticker ORDER BY p.price_date) - 1
      FROM `project-e042f011-a587-4cbe-8f7.Market_Data_Project.fact_daily_prices` p
      JOIN `project-e042f011-a587-4cbe-8f7.Market_Data_Project.tickers` t ON t.ticker = p.ticker
      WHERE t.index_name = 'S&P 500' AND t.is_active AND p.close_price > 0)
    WHERE ret IS NOT NULL AND ABS(ret) < 0.4      -- drops data glitches and stock splits
    QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker, d ORDER BY pref) = 1)
),
ew AS (
  SELECT d, AVG(ret) AS ew_ret, COUNT(*) AS n_stocks FROM us_ret GROUP BY d HAVING COUNT(*) >= 100
),
etf AS (   -- RSP (equal-weight S&P 500 fund) and SPY (the normal S&P 500 fund): the cleanest measure
  SELECT price_date AS d, MAX(IF(ticker = 'RSP', close, NULL)) AS rsp, MAX(IF(ticker = 'SPY', close, NULL)) AS spy
  FROM `project-e042f011-a587-4cbe-8f7.Market_Data_Project.hist_prices_20y_us`
  WHERE ticker IN ('RSP', 'SPY') AND close > 0 GROUP BY 1 HAVING COUNT(*) = 2
),
etf_rel AS (
  SELECT d, 100 * (rsp / LAG(rsp, 126) OVER (ORDER BY d) - spy / LAG(spy, 126) OVER (ORDER BY d)) AS rel FROM etf
),
breadth_us AS (
  -- prefer the two funds; fall back to our own average of today's S&P 500 members on days the fund prices
  -- haven't loaded (that fallback can be several points out, because it only holds today's members)
  SELECT s.d,
         COALESCE(f.rel, 100 * (EXP(SUM(LN(1 + IFNULL(e.ew_ret, 0))) OVER (ORDER BY s.d ROWS BETWEEN 125 PRECEDING AND CURRENT ROW)) - 1
                                - (s.close / LAG(s.close, 126) OVER (ORDER BY s.d) - 1))) AS rel,
         IF(f.rel IS NULL, 1, 0) AS used_fallback
  FROM spx s LEFT JOIN ew e ON e.d = s.d LEFT JOIN etf_rel f ON f.d = s.d
),

-- ---------------------------------------------------------------- 6. UK breadth: share of FTSE 350 stocks above their 200-day average
uk_px AS (
  SELECT ticker, d, close FROM (
    SELECT ticker, price_date AS d, close, 2 AS pref
    FROM `project-e042f011-a587-4cbe-8f7.Market_Data_Project.hist_prices_20y`
    WHERE index_name IN ('FTSE 100', 'FTSE 250') AND close > 0
    UNION ALL
    SELECT p.ticker, p.price_date, CAST(p.close_price AS FLOAT64), 1
    FROM `project-e042f011-a587-4cbe-8f7.Market_Data_Project.fact_daily_prices` p
    JOIN `project-e042f011-a587-4cbe-8f7.Market_Data_Project.tickers` t ON t.ticker = p.ticker
    WHERE t.index_name IN ('FTSE 100', 'FTSE 250') AND t.is_active AND p.close_price > 0)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker, d ORDER BY pref) = 1
),
uk_ma AS (
  SELECT ticker, d, close,
         AVG(close) OVER (PARTITION BY ticker ORDER BY d ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS ma200,
         COUNT(*) OVER (PARTITION BY ticker ORDER BY d ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS n200
  FROM uk_px
),
breadth_uk AS (
  SELECT d, 100 * AVG(IF(close > ma200, 1, 0)) AS pct_above, COUNT(*) AS n_stocks
  FROM uk_ma WHERE n200 = 200 GROUP BY d HAVING COUNT(*) >= 100
),

-- ---------------------------------------------------------------- 7. one row per day per indicator
readings AS (
  SELECT dy.d, 'R1' AS indicator_id, dy.us_curve AS value, dy.us_curve_dt AS data_as_of,
         c.us_inv_days_2y AS n_aux, c.us_inv_6m AS flag_aux FROM daily dy JOIN curves c ON c.d = dy.d
  UNION ALL
  SELECT dy.d, 'R2', dy.uk_curve, dy.gilt_dt, c.uk_inv_days_2y, c.uk_inv_6m FROM daily dy JOIN curves c ON c.d = dy.d
  UNION ALL
  SELECT d, 'C1', hy, hy_dt, NULL, NULL FROM daily
  UNION ALL
  SELECT d, 'J1', sahm, sahm_dt, NULL, NULL FROM daily
  UNION ALL
  SELECT d, 'J2', uk_gap, uk_dt, NULL, NULL FROM daily
  UNION ALL
  SELECT d, 'J3', claims_rise, claims_dt, NULL, NULL FROM daily
  UNION ALL
  SELECT d, 'S1', vix, vix_dt, NULL, NULL FROM daily
  UNION ALL
  SELECT d, 'V1', buffett, buffett_dt, NULL, NULL FROM daily
  UNION ALL
  SELECT b.d, 'B1', b.rel, b.d, b.used_fallback, NULL FROM breadth_us b JOIN cal ON cal.d = b.d
  UNION ALL
  SELECT b.d, 'B2', b.pct_above, b.d, b.n_stocks, NULL FROM breadth_uk b JOIN cal ON cal.d = b.d
  UNION ALL
  SELECT x.d, IF(x.symbol = '^FTSE', 'D1', 'D2'), x.n25, x.d, NULL, NULL FROM dist x JOIN cal ON cal.d = x.d
),
scored AS (
  SELECT r.*, i.sort, i.area, i.name, i.unit, i.amber_level, i.red_level,
         dy.hy_chg3m, dy.uk_unemp, dy.claims_ma4, dy.buffett_q, dy.gilt10, dy.bank_rate,
         r.value - LAG(r.value, 5) OVER (PARTITION BY r.indicator_id ORDER BY r.d) AS value_change,
         ROUND(100 * PERCENT_RANK() OVER (PARTITION BY r.indicator_id, r.value IS NULL ORDER BY r.value), 0) AS pctile_20y
  FROM readings r
  JOIN `project-e042f011-a587-4cbe-8f7.Market_Data_Project.ew_indicators` i USING (indicator_id)
  LEFT JOIN daily dy ON dy.d = r.d
)
SELECT
  d AS warning_date, indicator_id, sort, area, name, unit,
  ROUND(value, 2) AS value, ROUND(value_change, 2) AS value_change,
  IF(value IS NULL, NULL, pctile_20y) AS pctile_20y, data_as_of,
  CASE
    WHEN value IS NULL THEN 'NO DATA'
    -- yield curves: red once positive again after a long inversion; amber when flat or inverted
    WHEN indicator_id IN ('R1', 'R2') AND value >= red_level AND n_aux >= 60 AND flag_aux = 1 THEN 'RED'
    WHEN indicator_id IN ('R1', 'R2') AND value < amber_level THEN 'AMBER'
    -- junk spread: level or speed of widening
    WHEN indicator_id = 'C1' AND (value > red_level OR hy_chg3m >= 2.0) THEN 'RED'
    WHEN indicator_id = 'C1' AND (value > amber_level OR hy_chg3m >= 1.0) THEN 'AMBER'
    -- Buffett indicator: amber only, by percentile
    WHEN indicator_id = 'V1' AND pctile_20y >= amber_level THEN 'AMBER'
    -- "low is bad" indicators
    WHEN indicator_id = 'B1' AND value <= red_level THEN 'RED'
    WHEN indicator_id = 'B2' AND value < red_level THEN 'RED'
    WHEN indicator_id = 'B1' AND value <= amber_level THEN 'AMBER'
    WHEN indicator_id = 'B2' AND value < amber_level THEN 'AMBER'
    -- everything else: "high is bad"
    WHEN indicator_id IN ('J1', 'J2', 'J3', 'S1', 'D1', 'D2') AND value >= red_level THEN 'RED'
    WHEN indicator_id IN ('J1', 'J2', 'J3', 'S1', 'D1', 'D2') AND value >= amber_level THEN 'AMBER'
    ELSE 'GREEN'
  END AS light,
  CASE indicator_id
    WHEN 'R1' THEN FORMAT('10-year minus 3-month is %+.2f points; inverted on %d of the last 504 trading days.', value, n_aux)
    WHEN 'R2' THEN FORMAT('10-year gilt %.2f%% minus Bank Rate %.2f%% = %+.2f points; inverted on %d of the last 504 trading days.', gilt10, bank_rate, value, n_aux)
    WHEN 'C1' THEN FORMAT('Junk bonds pay %.2f%% over Treasuries, %+.2f points over 3 months.', value, IFNULL(hy_chg3m, 0))
    WHEN 'J1' THEN FORMAT('US unemployment (3-month average) is %.2f points above its 12-month low.', value)
    WHEN 'J2' THEN FORMAT('UK unemployment %.1f%% is %.1f points above its 12-month low.', uk_unemp, value)
    WHEN 'J3' THEN FORMAT('New claims average %s a week, %.0f%% above the 12-month low.', FORMAT("%'d", CAST(claims_ma4 AS INT64)), value)
    WHEN 'S1' THEN FORMAT('VIX at %.1f (long-run typical level is about 17).', value)
    WHEN 'V1' THEN FORMAT('US shares are worth %.0f%% of GDP, higher than %.0f%% of readings since 2005.', value, pctile_20y)
    WHEN 'B1' THEN FORMAT('Over 6 months the average S&P 500 stock has done %+.1f points versus the index%s.', value,
                          IF(n_aux = 1, ' (own estimate: fund prices not loaded)', ''))
    WHEN 'B2' THEN FORMAT('%.0f%% of FTSE 100 and 250 stocks are above their 200-day average.', value)
    WHEN 'D1' THEN FORMAT('%d heavy-volume down days for the FTSE 100 in the last 25 sessions.', CAST(value AS INT64))
    WHEN 'D2' THEN FORMAT('%d heavy-volume down days for the S&P 500 in the last 25 sessions.', CAST(value AS INT64))
  END AS detail
FROM scored
