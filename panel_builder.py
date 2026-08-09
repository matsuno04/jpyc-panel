# -*- coding: utf-8 -*-
"""
panel_builder.py — 生イベント(parquet)から研究用データセット一式を構築する。

使い方:
    python panel_builder.py                # チェーン別 + 全チェーン統合(combined)
    python panel_builder.py polygon        # 1チェーンのみ

入力:  data/events_{chain}.parquet, data/meta_{chain}.json, known_addresses.csv(任意)
出力(output/):
    daily_panel_{scope}.csv        … 日次パネル(本体)
    address_master_{scope}.csv     … アドレス台帳(first_seen, 累計入出金, 相手数, フラグ)
    cohort_retention_{scope}.csv   … 獲得週コホート × 経過週 の保有継続率
    balances_latest_{scope}.csv    … 最新残高スナップショット
    flag_candidates_{scope}.csv    … CEX/運営アドレス候補(手動ラベリング用)

残高は整数(最小単位)のまま更新し、丸め誤差を回避している。
"""
import os
import sys
import json
import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows既定(cp932)だと⚠等でUnicodeEncodeErrorになるため

from config import (ZERO_ADDRESS, DUST_THRESHOLDS, BALANCE_BUCKETS,
                    RETENTION_THRESHOLD, DATA_DIR, OUT_DIR, CHAINS)


# ---------------------------------------------------------------- utilities
def gini(x):
    """残高>0の配列に対するジニ係数。"""
    x = np.sort(np.asarray(x, dtype=np.float64))
    n = len(x)
    s = x.sum()
    if n == 0 or s == 0:
        return np.nan
    cum = np.cumsum(x)
    return float((n + 1 - 2 * (cum / s).sum()) / n)


def load_known_addresses(path="known_addresses.csv"):
    """手動ラベル済みアドレス(運営・CEX等)。category列が 'exclude' の行を除外対象にする。"""
    if not os.path.exists(path):
        return pd.DataFrame(columns=["address", "label", "category"]), set()
    df = pd.read_csv(path, comment="#")
    df["address"] = df["address"].str.lower().str.strip()
    excl = set(df.loc[df["category"].str.lower() == "exclude", "address"])
    return df, excl


def load_dex_hub_addresses(path="dex_hub_addresses.csv"):
    """DEXルーター/プール等の中継アドレス(取引量が極端に多く、実ユーザーの
    保有・普及行動の分析対象外とみなすもの)。known_addresses.csvのexclude
    (運営ウォレット)とは別枠: 発行/償還/internal分類には使わず、
    保有者・アクティブアドレス集計(_ex_hub列)からのみ除外する。"""
    if not os.path.exists(path):
        return set()
    df = pd.read_csv(path, comment="#")
    return set(df["address"].str.lower().str.strip())


def load_events(chains):
    frames = []
    decimals = None
    for c in chains:
        p = os.path.join(DATA_DIR, f"events_{c}.parquet")
        m = os.path.join(DATA_DIR, f"meta_{c}.json")
        if not os.path.exists(p):
            print(f"  (skip: {p} なし)")
            continue
        df = pd.read_parquet(p)
        df["chain"] = c
        with open(m) as f:
            meta = json.load(f)
        d = meta.get("decimals", 18)
        if decimals is None:
            decimals = d
        elif decimals != d:
            raise ValueError("チェーン間で decimals が不一致。builder の修正が必要。")
        frames.append(df)
    if not frames:
        raise FileNotFoundError("イベントファイルがありません。先に collector.py を実行してください。")
    ev = pd.concat(frames, ignore_index=True)
    ev = ev.sort_values(["ts", "chain", "block", "log_index"]).reset_index(drop=True)
    # ts(epoch秒)はUTC基準。日次集計の日境界はJST(UTC+9)で区切りたいので、
    # 一旦UTCとして明示(tz_localize)してからJSTに変換(tz_convert)し、
    # その上で日付だけを取り出す。ts列自体はUTC epoch秒のまま変更しない。
    ev["date"] = (
        pd.to_datetime(ev["ts"], unit="s")
        .dt.tz_localize("UTC")
        .dt.tz_convert("Asia/Tokyo")
        .dt.date
    )
    return ev, (decimals if decimals is not None else 18)


def build_summary(panel):
    """サイトの「スナップショット」欄向けに、最新日の要約値をまとめる。"""
    if panel.empty:
        return {}
    last = panel.iloc[-1]
    prior = panel.iloc[-8] if len(panel) > 7 else None  # 7日(1週間)前の行

    def wow_pct(col):
        """先週比の増減率(7日前との比較)。データが7日未満、または7日前が0の場合はNone。"""
        if prior is None:
            return None
        pv = prior[col]
        if pd.isna(pv) or pv == 0:
            return None
        return float((last[col] - pv) / pv)

    summary = {
        "date": str(last["date"]),
        "circulating_supply": float(last["circulating_supply"]),
        "circulating_supply_wow_pct": wow_pct("circulating_supply"),
        "cumulative_mint_volume": float(panel["mint_volume"].sum()),
        "holders_gt0": int(last["holders_gt0"]),
        "holders_gt0_wow_pct": wow_pct("holders_gt0"),
        "holders_ge100": int(last.get("holders_ge100", 0)),
        "holders_ge1000": int(last.get("holders_ge1000", 0)),
        "holders_ge10000": int(last.get("holders_ge10000", 0)),
        "holders_ge1000000": int(last.get("n_ge_1m", 0)),
        "total_participants": int(panel["new_addresses"].sum()),
    }
    # DEXハブ除外版(dex_hub_addresses.csvがあるスコープのみ列が存在する)。
    # ダッシュボードのスナップショット欄で「無印(括弧内: DEXハブ除く)」と補足表示するため。
    if "circulating_supply_ex_hub" in panel.columns:
        summary["circulating_supply_ex_hub"] = float(last["circulating_supply_ex_hub"])
    if "holders_gt0_ex_hub" in panel.columns:
        summary["holders_gt0_ex_hub"] = int(last["holders_gt0_ex_hub"])
    return summary


# ---------------------------------------------------------------- core build
def build(scope_name, ev, decimals, exclude, reviewed=frozenset(), dex_hub=frozenset()):
    """dex_hub: DEXルーター/プール等(known_addresses.csvのexclude=運営とは別枠)。
    発行/償還/internal分類には使わない。保有者・アクティブアドレス系の指標は、
    既存の列(exclude=運営のみ除外、DEXハブ含む「広い普及」の指標)に加えて、
    同じ指標に _ex_hub を付けた列(exclude+dex_hub除外、「直接的なP2P利用」の
    指標)を追加出力する。既存列の意味・値は変更しない(後方互換)。"""
    unit = 10 ** decimals
    thr_units = [int(t * unit) for t in DUST_THRESHOLDS]  # 整数比較用
    hub_excl = exclude | dex_hub

    balances = {}       # address -> int 残高(最小単位)
    first_seen = {}     # address -> date (初めて残高>0になった日)
    first_week = {}     # address -> 獲得週 (ISO週の月曜日)
    last_active = {}
    ever_seen = set()

    daily_rows = []
    cohort_rows = []    # (week_observed, cohort_week, still_holding, cohort_size)

    # アドレス台帳用の累積カウンタ
    agg_in = {}; agg_out = {}; n_in = {}; n_out = {}
    got_mint = set(); did_burn = set()
    cparty_in = {}; cparty_out = {}   # ユニーク相手方(集合はメモリ的に重いのでdict of set)

    dates = sorted(ev["date"].unique())
    grouped = ev.groupby("date", sort=True)

    prev_week_marker = None

    for d in dates:
        day = grouped.get_group(d)
        touched = {}

        n_mint = n_burn = 0
        v_mint = v_burn = v_p2p = 0
        # p2p_volumeの内訳(運営ウォレット=exclude登録アドレスとの関係で4分類)
        # 発行=運営→一般、償還=一般→運営、internal=運営↔運営、取引=一般↔一般(純粋なP2P)
        n_issuance = n_redemption = n_internal = n_transfer = 0
        v_issuance = v_redemption = v_internal = v_transfer = 0
        senders = set(); receivers = set()
        senders_ex = set(); receivers_ex = set()

        for frm, to, vr in zip(day["from"].values, day["to"].values,
                               day["value_raw"].values):
            v = int(vr)
            is_mint = (frm == ZERO_ADDRESS)
            is_burn = (to == ZERO_ADDRESS)

            if not is_mint:
                if frm not in touched:
                    touched[frm] = balances.get(frm, 0)
                balances[frm] = balances.get(frm, 0) - v
                agg_out[frm] = agg_out.get(frm, 0) + v
                n_out[frm] = n_out.get(frm, 0) + 1
                cparty_out.setdefault(frm, set()).add(to)
                last_active[frm] = d
                if frm not in exclude:
                    senders.add(frm)
                if frm not in hub_excl:
                    senders_ex.add(frm)
            if not is_burn:
                if to not in touched:
                    touched[to] = balances.get(to, 0)
                balances[to] = balances.get(to, 0) + v
                agg_in[to] = agg_in.get(to, 0) + v
                n_in[to] = n_in.get(to, 0) + 1
                cparty_in.setdefault(to, set()).add(frm)
                last_active[to] = d
                if to not in exclude:
                    receivers.add(to)
                if to not in hub_excl:
                    receivers_ex.add(to)

            if is_mint:
                n_mint += 1; v_mint += v
                got_mint.add(to)
            elif is_burn:
                n_burn += 1; v_burn += v
                did_burn.add(frm)
            else:
                v_p2p += v
                frm_op = frm in exclude
                to_op = to in exclude
                if frm_op and to_op:
                    n_internal += 1; v_internal += v
                elif frm_op and not to_op:
                    n_issuance += 1; v_issuance += v
                elif to_op and not frm_op:
                    n_redemption += 1; v_redemption += v
                else:
                    n_transfer += 1; v_transfer += v

        # --- 当日の遷移(new / zeroed / resurrected) ---
        new_a = zeroed_a = resurrected_a = 0
        new_a_ex = zeroed_a_ex = resurrected_a_ex = 0
        for a, pre in touched.items():
            post = balances.get(a, 0)
            if a in exclude or a == ZERO_ADDRESS:
                continue
            is_hub = a in dex_hub
            if post > 0 and a not in ever_seen:
                ever_seen.add(a)
                first_seen[a] = d
                iso = pd.Timestamp(d).to_period("W").start_time.date()
                first_week[a] = iso
                new_a += 1
                if not is_hub:
                    new_a_ex += 1
            elif pre <= 0 and post > 0 and a in ever_seen:
                resurrected_a += 1
                if not is_hub:
                    resurrected_a_ex += 1
            elif pre > 0 and post <= 0 and a in ever_seen:
                zeroed_a += 1
                if not is_hub:
                    zeroed_a_ex += 1

        # --- 日末スナップショット統計 ---
        # "全体"(運営のみ除外、DEXハブ含む=広い普及の指標。既存列、後方互換のため不変)
        pos = np.array([v for a, v in balances.items()
                        if v > 0 and a not in exclude and a != ZERO_ADDRESS],
                       dtype=np.float64) / unit
        pos_sorted = np.sort(pos)[::-1] if len(pos) else pos
        total_bal = pos.sum()
        # "_ex_hub"(運営+DEXハブ除外=直接的なP2P利用の指標。新規追加列)
        pos_ex = np.array([v for a, v in balances.items()
                           if v > 0 and a not in hub_excl and a != ZERO_ADDRESS],
                          dtype=np.float64) / unit
        pos_ex_sorted = np.sort(pos_ex)[::-1] if len(pos_ex) else pos_ex
        total_bal_ex = pos_ex.sum()

        row = {
            "date": d,
            "transfers": len(day),
            "mint_count": n_mint, "burn_count": n_burn,
            "mint_volume": v_mint / unit, "burn_volume": v_burn / unit,
            "p2p_volume": v_p2p / unit,
            "issuance_count": n_issuance, "issuance_volume": v_issuance / unit,
            "redemption_count": n_redemption, "redemption_volume": v_redemption / unit,
            "internal_count": n_internal, "internal_volume": v_internal / unit,
            "transfer_count": n_transfer, "transfer_volume": v_transfer / unit,
            "unique_senders": len(senders),
            "unique_receivers": len(receivers),
            "active_addresses": len(senders | receivers),
            "new_addresses": new_a,
            "zeroed_addresses": zeroed_a,
            "resurrected_addresses": resurrected_a,
            "holders_gt0": int(len(pos)),
            "circulating_supply": total_bal,
            "mean_balance": float(pos.mean()) if len(pos) else np.nan,
            "median_balance": float(np.median(pos)) if len(pos) else np.nan,
            "gini": gini(pos),
            "top10_share": float(pos_sorted[:10].sum() / total_bal) if total_bal > 0 else np.nan,
            "top100_share": float(pos_sorted[:100].sum() / total_bal) if total_bal > 0 else np.nan,
            # ---- ここから DEXハブ除外版(_ex_hub) ----
            "unique_senders_ex_hub": len(senders_ex),
            "unique_receivers_ex_hub": len(receivers_ex),
            "active_addresses_ex_hub": len(senders_ex | receivers_ex),
            "new_addresses_ex_hub": new_a_ex,
            "zeroed_addresses_ex_hub": zeroed_a_ex,
            "resurrected_addresses_ex_hub": resurrected_a_ex,
            "holders_gt0_ex_hub": int(len(pos_ex)),
            "circulating_supply_ex_hub": total_bal_ex,
            "mean_balance_ex_hub": float(pos_ex.mean()) if len(pos_ex) else np.nan,
            "median_balance_ex_hub": float(np.median(pos_ex)) if len(pos_ex) else np.nan,
            "gini_ex_hub": gini(pos_ex),
            "top10_share_ex_hub": float(pos_ex_sorted[:10].sum() / total_bal_ex) if total_bal_ex > 0 else np.nan,
            "top100_share_ex_hub": float(pos_ex_sorted[:100].sum() / total_bal_ex) if total_bal_ex > 0 else np.nan,
        }
        for t, tu in zip(DUST_THRESHOLDS, thr_units):
            row[f"holders_ge{int(t)}"] = int((pos >= t).sum())
            row[f"holders_ge{int(t)}_ex_hub"] = int((pos_ex >= t).sum())
        for name, lo, hi in BALANCE_BUCKETS:
            m = (pos >= lo) & (pos < hi)
            row[f"n_{name}"] = int(m.sum())
            row[f"val_{name}"] = float(pos[m].sum())
            m_ex = (pos_ex >= lo) & (pos_ex < hi)
            row[f"n_{name}_ex_hub"] = int(m_ex.sum())
            row[f"val_{name}_ex_hub"] = float(pos_ex[m_ex].sum())
        row["share_holders_lt10k"] = (
            (row["n_lt_1k"] + row["n_1k_10k"]) / row["holders_gt0"]
            if row["holders_gt0"] else np.nan)
        row["share_holders_lt10k_ex_hub"] = (
            (row["n_lt_1k_ex_hub"] + row["n_1k_10k_ex_hub"]) / row["holders_gt0_ex_hub"]
            if row["holders_gt0_ex_hub"] else np.nan)
        daily_rows.append(row)

        # --- 週次コホート保持(週の変わり目 or 最終日に記録) ---
        wk = pd.Timestamp(d).to_period("W").start_time.date()
        is_last = (d == dates[-1])
        if (prev_week_marker is not None and wk != prev_week_marker) or is_last:
            obs_week = prev_week_marker if (wk != prev_week_marker and not is_last) else wk
            hold_by_cohort = {}
            ret_thr = RETENTION_THRESHOLD * unit
            for a, v in balances.items():
                if v >= ret_thr and a not in exclude and a in first_week:
                    cw = first_week[a]
                    hold_by_cohort[cw] = hold_by_cohort.get(cw, 0) + 1
            size_by_cohort = {}
            for a, cw in first_week.items():
                size_by_cohort[cw] = size_by_cohort.get(cw, 0) + 1
            for cw, size in size_by_cohort.items():
                cohort_rows.append({
                    "observed_week": obs_week, "cohort_week": cw,
                    "cohort_size": size,
                    "still_holding": hold_by_cohort.get(cw, 0),
                })
        prev_week_marker = wk

    # ---------------------------------------------------------------- 出力
    os.makedirs(OUT_DIR, exist_ok=True)
    panel = pd.DataFrame(daily_rows)
    panel.to_csv(os.path.join(OUT_DIR, f"daily_panel_{scope_name}.csv"), index=False)

    with open(os.path.join(OUT_DIR, f"summary_{scope_name}.json"), "w",
              encoding="utf-8") as f:
        json.dump(build_summary(panel), f, ensure_ascii=False, indent=1, default=str)

    coh = pd.DataFrame(cohort_rows)
    if len(coh):
        coh["weeks_since"] = ((pd.to_datetime(coh["observed_week"])
                               - pd.to_datetime(coh["cohort_week"])).dt.days // 7)
        coh = coh[coh["weeks_since"] >= 0]
        coh["retention"] = coh["still_holding"] / coh["cohort_size"]
        coh.to_csv(os.path.join(OUT_DIR, f"cohort_retention_{scope_name}.csv"),
                   index=False)

    # アドレス台帳
    addrs = sorted(ever_seen | set(balances))
    unit_f = float(unit)
    master = pd.DataFrame({
        "address": addrs,
        "first_seen": [first_seen.get(a) for a in addrs],
        "last_active": [last_active.get(a) for a in addrs],
        "balance": [balances.get(a, 0) / unit_f for a in addrs],
        "total_in": [agg_in.get(a, 0) / unit_f for a in addrs],
        "total_out": [agg_out.get(a, 0) / unit_f for a in addrs],
        "n_tx_in": [n_in.get(a, 0) for a in addrs],
        "n_tx_out": [n_out.get(a, 0) for a in addrs],
        "n_counterparties_in": [len(cparty_in.get(a, ())) for a in addrs],
        "n_counterparties_out": [len(cparty_out.get(a, ())) for a in addrs],
        "received_mint": [a in got_mint for a in addrs],
        "sent_burn": [a in did_burn for a in addrs],
        "excluded": [a in exclude for a in addrs],
        "is_dex_hub": [a in dex_hub for a in addrs],
    })
    master.to_csv(os.path.join(OUT_DIR, f"address_master_{scope_name}.csv"),
                  index=False)

    # 最新残高スナップショット
    snap = master[master["balance"] > 0][["address", "balance", "first_seen",
                                          "last_active", "excluded"]]
    snap.sort_values("balance", ascending=False).to_csv(
        os.path.join(OUT_DIR, f"balances_latest_{scope_name}.csv"), index=False)

    # フラグ候補: 相手方数・取扱量の上位(CEXホットウォレット/運営の候補)
    # + ミント受取アドレスは取扱量が少なくても必ず候補に入れる(中継アドレス等の見落とし防止)
    # known_addresses.csv に既に登録済み(exclude/watch問わず)のアドレスは、
    # 確認済みとして候補から除く → 残るのは「まだ見ていない新顔」だけになる
    cand = master.copy()
    cand["degree"] = cand["n_counterparties_in"] + cand["n_counterparties_out"]
    cand["turnover"] = cand["total_in"] + cand["total_out"]
    cand = cand[~cand["address"].isin(reviewed)]
    top = cand.sort_values(["degree", "turnover"], ascending=False).head(40)
    mint_addrs = cand[cand["received_mint"]]
    cand = pd.concat([top, mint_addrs]).drop_duplicates(subset="address")
    cand = cand.sort_values(["received_mint", "degree", "turnover"], ascending=False)
    cand[["address", "degree", "turnover", "balance", "n_tx_in", "n_tx_out",
          "received_mint", "sent_burn", "first_seen"]].to_csv(
        os.path.join(OUT_DIR, f"flag_candidates_{scope_name}.csv"), index=False)

    # --- データ完全性チェック ---
    # ERC-20では残高は負にならない。負残高のアドレスが存在する場合、
    # イベントの取得漏れ(走査範囲の欠落・シャード破損)を意味する。
    neg = int((master["balance"] < -1e-9).sum())
    if neg > 0:
        print(f"[{scope_name}] [WARN] 負残高アドレス {neg} 件 — Transferイベントの取得漏れの疑い。"
              f" checkpointを削除して collector.py を再実行することを推奨。")

    # --- 未確認のミント受取アドレス警告 ---
    # ミントを受け取ったことのあるアドレスは運営(発行体)側である可能性が高い。
    # known_addresses.csv に未登録のものが残っていると、流通量計算に紛れ込む恐れがある。
    unreviewed_mint = sorted(a for a in got_mint if a not in reviewed)
    if unreviewed_mint:
        print(f"[{scope_name}] [WARN] 未確認のミント受取アドレスが {len(unreviewed_mint)} 件あります: "
              f"{', '.join(unreviewed_mint)}")
        print(f"[{scope_name}]        → block explorerで確認し、運営/取引所等であれば"
              f"known_addresses.csv に追記してください(flag_candidates_{scope_name}.csvにも掲載)。")

    print(f"[{scope_name}] panel: {len(panel)}日  addresses: {len(master):,}  "
          f"holders(latest): {panel['holders_gt0'].iloc[-1]:,}")
    return panel


def main(chains=None):
    chains = chains or [c for c in CHAINS
                        if os.path.exists(os.path.join(DATA_DIR, f"events_{c}.parquet"))]
    known, exclude = load_known_addresses()
    dex_hub = load_dex_hub_addresses()
    reviewed = set(known["address"])  # exclude/watch問わず、既に確認済みのアドレス
    print(f"除外アドレス(known_addresses.csv, category=exclude): {len(exclude)}件")
    print(f"DEXハブ除外(dex_hub_addresses.csv, 集計のみ・発行償還分類には不使用): {len(dex_hub)}件")

    # チェーン別
    for c in chains:
        ev, dec = load_events([c])
        build(c, ev, dec, exclude, reviewed, dex_hub)

    # 全チェーン統合: 同一アドレスはEVM系では同一の鍵保有者である可能性が高いので、
    # 残高をアドレス単位でチェーン横断合算した"combined"を作る(ユニーク保有者の近似)。
    if len(chains) > 1:
        ev, dec = load_events(chains)
        build("combined", ev, dec, exclude, reviewed, dex_hub)


if __name__ == "__main__":
    main(sys.argv[1:] or None)
