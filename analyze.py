# -*- coding: utf-8 -*-
"""
analyze.py — daily_panel / cohort_retention から標準図表(PNG)を生成する。

使い方:
    python analyze.py combined      # scope名(combined / ethereum / polygon / ...)
    python analyze.py               # combined があればそれ、なければ最初に見つかったもの

図はすべて output/figs_{scope}/ に保存される。
combined を指定した場合のみ、4チェーンを横並びで比較する図(09, 10)も追加で生成される。

イベント注釈:
    リポジトリ直下に events.csv (列: date, label, category) を置くと、
    主要な時系列グラフに縦線とラベルで注釈が入る。ファイルが無ければ何もしない。
    category は "campaign" / "news" / "regulatory" を想定(色分け用。他の値は灰色)。

(日本語ラベルにしたい場合は Colab で `pip install japanize-matplotlib` して
 import japanize_matplotlib を先頭に足す。)
"""
import os
import sys
import glob
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows既定(cp932)での日本語出力の文字化け/エラーを防ぐ

from config import OUT_DIR, DUST_THRESHOLDS

EVENTS_PATH = "events.csv"
EVENT_CATEGORY_COLORS = {
    "campaign": "tab:orange",
    "news": "tab:purple",
    "regulatory": "tab:red",
}
EVENT_DEFAULT_COLOR = "gray"
CHAIN_NAMES = ["ethereum", "polygon", "avalanche", "kaia"]


def pick_scope():
    if len(sys.argv) > 1:
        return sys.argv[1]
    if os.path.exists(os.path.join(OUT_DIR, "daily_panel_combined.csv")):
        return "combined"
    hits = glob.glob(os.path.join(OUT_DIR, "daily_panel_*.csv"))
    if not hits:
        raise FileNotFoundError("output/ に daily_panel が見つかりません")
    return os.path.basename(hits[0])[len("daily_panel_"):-4]


def save(fig, name, fig_dir):
    fig.tight_layout()
    fig.savefig(os.path.join(fig_dir, name), dpi=150)
    plt.close(fig)
    print("  saved", name)


def load_events():
    """events.csv (date,label,category) を読み込む。無ければ None。"""
    if not os.path.exists(EVENTS_PATH):
        return None
    ev = pd.read_csv(EVENTS_PATH, comment="#", parse_dates=["date"])
    return ev


def annotate_events(ax, events, x_min, x_max):
    """時系列グラフに、期間内のイベントを縦線+ラベルで注釈する。"""
    if events is None or len(events) == 0:
        return
    in_range = events[(events["date"] >= x_min) & (events["date"] <= x_max)]
    if len(in_range) == 0:
        return
    ylim = ax.get_ylim()
    for _, row in in_range.iterrows():
        color = EVENT_CATEGORY_COLORS.get(
            str(row.get("category", "")).strip().lower(), EVENT_DEFAULT_COLOR)
        ax.axvline(row["date"], color=color, linestyle="--", alpha=.6, lw=1)
        ax.text(row["date"], ylim[1], f" {row['label']}", rotation=90,
                va="top", ha="right", fontsize=7, color=color, alpha=.85)


def plot_chain_comparison(fig_dir, events):
    """combined時のみ: 4チェーンの流通量・ホルダー数を横並びで比較する図を追加する。"""
    frames = {}
    for c in CHAIN_NAMES:
        path = os.path.join(OUT_DIR, f"daily_panel_{c}.csv")
        if os.path.exists(path):
            frames[c] = pd.read_csv(
                path, parse_dates=["date"])[["date", "circulating_supply", "holders_gt0"]]
    if len(frames) < 2:
        print("  (chain比較図はスキップ: 2チェーン以上のdaily_panelが必要)")
        return

    all_dates = sorted(set().union(*[set(df["date"]) for df in frames.values()]))
    idx = pd.DatetimeIndex(all_dates)

    supply = pd.DataFrame(index=idx)
    holders = pd.DataFrame(index=idx)
    for c, df in frames.items():
        s = df.set_index("date").reindex(idx)
        supply[c] = s["circulating_supply"].ffill().fillna(0)
        holders[c] = s["holders_gt0"].ffill().fillna(0)

    # 9. チェーン別流通量(面積グラフ) — どのチェーンが伸びているかの比較
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.stackplot(idx, [supply[c] for c in frames], labels=list(frames.keys()), alpha=.85)
    ax.set_title("Circulating supply by chain (combined)")
    ax.set_ylabel("JPYC")
    ax.legend(loc="upper left"); ax.grid(alpha=.3)
    annotate_events(ax, events, idx.min(), idx.max())
    save(fig, "09_chain_supply_share.png", fig_dir)

    # 10. チェーン別ホルダー数(折れ線)
    fig, ax = plt.subplots(figsize=(10, 5))
    for c in frames:
        ax.plot(idx, holders[c], label=c, lw=1.5)
    ax.set_title("Holders by chain (combined)")
    ax.set_ylabel("addresses")
    ax.legend(); ax.grid(alpha=.3)
    annotate_events(ax, events, idx.min(), idx.max())
    save(fig, "10_chain_holders.png", fig_dir)


def main():
    scope = pick_scope()
    fig_dir = os.path.join(OUT_DIR, f"figs_{scope}")
    os.makedirs(fig_dir, exist_ok=True)
    p = pd.read_csv(os.path.join(OUT_DIR, f"daily_panel_{scope}.csv"),
                    parse_dates=["date"])
    events = load_events()
    x_min, x_max = p.date.min(), p.date.max()

    # 1. ホルダー数(ダスト感度バンド) — "112k→60k"の分解に直結する図
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(p.date, p.holders_gt0, label="balance > 0", lw=2)
    for t in DUST_THRESHOLDS:
        col = f"holders_ge{int(t)}"
        if col in p:
            ax.plot(p.date, p[col], label=f">= {int(t):,} JPYC", lw=1.2)
    ax.set_title(f"Holders over time by dust threshold ({scope})")
    ax.set_ylabel("addresses"); ax.legend(); ax.grid(alpha=.3)
    annotate_events(ax, events, x_min, x_max)
    save(fig, "01_holders_dust_sensitivity.png", fig_dir)

    # 2. 新規獲得 vs 離脱(7日移動平均)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(p.date, p.new_addresses.rolling(7).mean(), label="new (7d MA)")
    ax.plot(p.date, p.zeroed_addresses.rolling(7).mean(), label="zeroed (7d MA)")
    ax.plot(p.date, p.resurrected_addresses.rolling(7).mean(),
            label="resurrected (7d MA)", alpha=.7)
    ax.set_title(f"Daily acquisition vs churn ({scope})")
    ax.set_ylabel("addresses/day"); ax.legend(); ax.grid(alpha=.3)
    annotate_events(ax, events, x_min, x_max)
    save(fig, "02_new_vs_churn.png", fig_dir)

    # 3. 保有分布(ホルダー数構成比)の推移
    bucket_cols = [c for c in p.columns if c.startswith("n_")]
    shares = p[bucket_cols].div(p["holders_gt0"], axis=0)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.stackplot(p.date, [shares[c] for c in bucket_cols],
                 labels=[c[2:] for c in bucket_cols], alpha=.85)
    ax.set_title(f"Holder composition by balance bucket ({scope})")
    ax.set_ylabel("share of holders"); ax.set_ylim(0, 1)
    ax.legend(loc="lower left", fontsize=8); ax.grid(alpha=.3)
    save(fig, "03_holder_buckets.png", fig_dir)

    # 4. 金額ベースの分布(どの層が価値を保有しているか)
    val_cols = [c for c in p.columns if c.startswith("val_")]
    vshares = p[val_cols].div(p["circulating_supply"], axis=0)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.stackplot(p.date, [vshares[c] for c in val_cols],
                 labels=[c[4:] for c in val_cols], alpha=.85)
    ax.set_title(f"Value composition by balance bucket ({scope})")
    ax.set_ylabel("share of supply"); ax.set_ylim(0, 1)
    ax.legend(loc="lower left", fontsize=8); ax.grid(alpha=.3)
    save(fig, "04_value_buckets.png", fig_dir)

    # 5. 集中度
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(p.date, p.gini, label="Gini")
    ax.plot(p.date, p.top10_share, label="Top-10 share")
    ax.plot(p.date, p.top100_share, label="Top-100 share")
    ax.set_title(f"Concentration ({scope})"); ax.set_ylim(0, 1)
    ax.legend(); ax.grid(alpha=.3)
    annotate_events(ax, events, x_min, x_max)
    save(fig, "05_concentration.png", fig_dir)

    # 6. 発行・償還・流通量
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(p.date, p.mint_volume.cumsum(), label="cumulative issuance (mint)")
    ax.plot(p.date, p.burn_volume.cumsum(), label="cumulative redemption (burn)")
    ax.plot(p.date, p.circulating_supply, label="circulating supply", lw=2)
    ax.set_title(f"Issuance / redemption / supply, JPYC ({scope})")
    ax.set_ylabel("JPYC"); ax.legend(); ax.grid(alpha=.3)
    annotate_events(ax, events, x_min, x_max)
    save(fig, "06_issuance_redemption.png", fig_dir)

    # 7. アクティビティ
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(p.date, p.transfers.rolling(7).mean(), label="transfers (7d MA)")
    ax.plot(p.date, p.active_addresses.rolling(7).mean(),
            label="active addresses (7d MA)")
    ax.set_title(f"Activity ({scope})"); ax.legend(); ax.grid(alpha=.3)
    annotate_events(ax, events, x_min, x_max)
    save(fig, "07_activity.png", fig_dir)

    # 8. コホート保持ヒートマップ
    cr_path = os.path.join(OUT_DIR, f"cohort_retention_{scope}.csv")
    if os.path.exists(cr_path):
        cr = pd.read_csv(cr_path, parse_dates=["cohort_week"])
        mat = cr.pivot_table(index="cohort_week", columns="weeks_since",
                             values="retention")
        fig, ax = plt.subplots(figsize=(12, max(4, len(mat) * .28)))
        im = ax.imshow(mat.values, aspect="auto", cmap="viridis",
                       vmin=0, vmax=1)
        ax.set_yticks(range(len(mat)))
        ax.set_yticklabels([d.strftime("%Y-%m-%d") for d in mat.index],
                           fontsize=7)
        ax.set_xticks(range(0, mat.shape[1], 2))
        ax.set_xticklabels(mat.columns[::2])
        ax.set_xlabel("weeks since acquisition")
        ax.set_title(f"Cohort retention (holding >= threshold) ({scope})")
        fig.colorbar(im, label="retention")
        save(fig, "08_cohort_retention.png", fig_dir)

    # combinedの場合のみ: チェーン間比較図を追加
    if scope == "combined":
        plot_chain_comparison(fig_dir, events)

    # サマリーをテキストで
    last = p.iloc[-1]
    print(f"\n=== {scope} 最新日 ({last.date.date()}) ===")
    print(f"holders>0: {last.holders_gt0:,.0f} / >=1000円: {last.get('holders_ge1000', float('nan')):,.0f}")
    print(f"circulating: {last.circulating_supply:,.0f} JPYC")
    print(f"share of holders <10k yen: {last.share_holders_lt10k:.1%}")
    print(f"gini: {last.gini:.3f}  top10: {last.top10_share:.1%}")


if __name__ == "__main__":
    main()
