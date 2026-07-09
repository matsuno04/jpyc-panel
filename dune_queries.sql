-- =====================================================================
-- JPYC クロスバリデーション用 Dune クエリ集
-- 自前パイプライン(collector.py)の結果と突き合わせて検証するためのもの。
-- dune.com で New Query → 貼り付けて実行。チェーンを変える場合は
-- erc20_ethereum → erc20_polygon / erc20_avalanche に変更。
-- (Kaia は Dune 対応状況を要確認。未対応なら Kaiascan で代替検証)
-- コントラクト: 0xe7c3d8c9a439fede00d2600032d5db0be71c3c29 (全チェーン共通)
-- =====================================================================

-- [Q1] 日次トランスファー統計(daily_panel の transfers / volume と照合)
SELECT
  date_trunc('day', evt_block_time)                            AS day,
  count(*)                                                     AS transfers,
  sum(value / 1e18)                                            AS volume_jpyc,
  count(DISTINCT "from")                                       AS unique_senders,
  count(DISTINCT "to")                                         AS unique_receivers,
  sum(CASE WHEN "from" = 0x0000000000000000000000000000000000000000
           THEN value / 1e18 ELSE 0 END)                       AS mint_volume,
  sum(CASE WHEN "to"   = 0x0000000000000000000000000000000000000000
           THEN value / 1e18 ELSE 0 END)                       AS burn_volume
FROM erc20_ethereum.evt_Transfer
WHERE contract_address = 0xe7c3d8c9a439fede00d2600032d5db0be71c3c29
GROUP BY 1
ORDER BY 1;

-- [Q2] 現時点のホルダー数と保有分布(balances_latest / daily_panel 最新行と照合)
WITH flows AS (
  SELECT "to"   AS address,  value / 1e18 AS amt
  FROM erc20_ethereum.evt_Transfer
  WHERE contract_address = 0xe7c3d8c9a439fede00d2600032d5db0be71c3c29
  UNION ALL
  SELECT "from" AS address, -value / 1e18 AS amt
  FROM erc20_ethereum.evt_Transfer
  WHERE contract_address = 0xe7c3d8c9a439fede00d2600032d5db0be71c3c29
),
bal AS (
  SELECT address, sum(amt) AS balance
  FROM flows
  WHERE address != 0x0000000000000000000000000000000000000000
  GROUP BY 1
  HAVING sum(amt) > 1e-9
)
SELECT
  count(*)                                        AS holders_gt0,
  count(CASE WHEN balance >= 1     THEN 1 END)    AS holders_ge1,
  count(CASE WHEN balance >= 1000  THEN 1 END)    AS holders_ge1000,
  count(CASE WHEN balance <  10000 THEN 1 END)    AS holders_lt10k,
  sum(balance)                                    AS circulating,
  approx_percentile(balance, 0.5)                 AS median_balance
FROM bal;

-- [Q3] 日次の新規アドレス数(初めてJPYCを受け取った日 / daily_panel の new_addresses と照合)
WITH first_rx AS (
  SELECT "to" AS address, min(evt_block_time) AS first_time
  FROM erc20_ethereum.evt_Transfer
  WHERE contract_address = 0xe7c3d8c9a439fede00d2600032d5db0be71c3c29
    AND "to" != 0x0000000000000000000000000000000000000000
  GROUP BY 1
)
SELECT date_trunc('day', first_time) AS day, count(*) AS new_addresses
FROM first_rx
GROUP BY 1
ORDER BY 1;
