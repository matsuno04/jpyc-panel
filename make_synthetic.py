# -*- coding: utf-8 -*-
"""テスト用の合成イベントを生成する(ネット接続不要でパイプラインを検証するため)。
   実データ取得後は不要。 python make_synthetic.py で data/ に events_*.parquet を作る。"""
import os, json
import numpy as np
import pandas as pd
from config import DATA_DIR, ZERO_ADDRESS

rng = np.random.default_rng(7)
os.makedirs(DATA_DIR, exist_ok=True)
UNIT = 10**18
START = pd.Timestamp("2025-10-27")
DAYS = 230

def addr(i, tag):
    return "0x" + f"{tag}{i:037x}"[-40:]

def gen_chain(chain, n_users, campaign_day, campaign_users):
    rows = []
    blk = 1000
    users = []
    issuer = addr(0, "aaa")  # 運営っぽい高頻度アドレス
    for d in range(DAYS):
        date = START + pd.Timedelta(days=d)
        ts0 = int(date.timestamp())
        # 通常の新規発行(mint→ユーザー)
        n_new = max(1, int(rng.poisson(n_users / DAYS)))
        if d == campaign_day:
            n_new += campaign_users  # キャンペーンでダスト大量獲得
        for _ in range(n_new):
            u = addr(len(users) + 1, "bbb")
            users.append(u)
            if d == campaign_day and rng.random() < .9:
                amt = int(rng.uniform(10, 500) * UNIT)      # ダスト
            else:
                amt = int(rng.lognormal(8, 2) * UNIT)       # 通常発行
            blk += 1
            rows.append((blk, 0, f"0xtx{chain}{blk}", ZERO_ADDRESS, u, str(amt),
                         ts0 + int(rng.uniform(0, 86000))))
        # P2P送金
        if len(users) > 10:
            for _ in range(int(rng.poisson(len(users) * 0.02))):
                a, b = rng.choice(len(users), 2, replace=False)
                amt = int(rng.lognormal(6, 1.5) * UNIT)
                blk += 1
                rows.append((blk, 0, f"0xtx{chain}{blk}", users[a], users[b],
                             str(amt), ts0 + int(rng.uniform(0, 86000))))
        # キャンペーン組の離脱(全額をissuerに送って残高ゼロ化 → 実質burn相当も混ぜる)
        if campaign_day < d < campaign_day + 60 and len(users) > campaign_users:
            for _ in range(int(rng.poisson(campaign_users / 60))):
                i = int(rng.integers(len(users) - campaign_users, len(users)))
                blk += 1
                # 便宜上「全額burn」で表現
                rows.append((blk, 0, f"0xtx{chain}{blk}", users[i], ZERO_ADDRESS,
                             "1", ts0 + int(rng.uniform(0, 86000))))
    df = pd.DataFrame(rows, columns=["block", "log_index", "tx_hash",
                                     "from", "to", "value_raw", "ts"])
    # 「全額burn」を正しくするため、残高を再現して修正するのは面倒なので
    # burn額はその時点の残高に一致させる後処理を行う
    bal = {}
    fixed = []
    for blk_, li_, tx_, frm_, to_, vr_, ts_ in (
            df.sort_values(["ts", "block"]).itertuples(index=False, name=None)):
        v = int(vr_)
        if to_ == ZERO_ADDRESS:
            v = bal.get(frm_, 0)  # 全額償還
            if v <= 0:
                continue
        if frm_ != ZERO_ADDRESS:
            bal[frm_] = bal.get(frm_, 0) - v
        if to_ != ZERO_ADDRESS:
            bal[to_] = bal.get(to_, 0) + v
        fixed.append((blk_, li_, tx_, frm_, to_, str(v), ts_))
    out = pd.DataFrame(fixed, columns=df.columns)
    out.to_parquet(os.path.join(DATA_DIR, f"events_{chain}.parquet"), index=False)
    with open(os.path.join(DATA_DIR, f"meta_{chain}.json"), "w") as f:
        json.dump({"decimals": 18, "deploy_block": 1000, "n_events": len(out)}, f)
    print(chain, len(out), "events")

gen_chain("polygon", 40000, 35, 30000)   # 12月頭に大キャンペーン→その後離脱
gen_chain("avalanche", 8000, 35, 3000)
