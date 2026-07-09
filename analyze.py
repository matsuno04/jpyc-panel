# -*- coding: utf-8 -*-
"""
analyze.py — daily_panel / cohort_retention から標準図表(PNG)を生成する。

使い方:
    python analyze.py combined      # scope名(combined / ethereum / polygon / ...)
    python analyze.py               # combined があればそれ、なければ最初に見つかったもの

図はすべて output/figs_{scope}/ に保存される。日本語ラベルはWindowsなら Yu Gothic、
Linux(GitHub Actions)なら IPAGothic(事前に apt でインストール)を使う。
combined を指定した場合のみ、4チェーンを横並びで比較する図(09, 10)も追加で生成される。

イベント注釈:
    リポジトリ直下に events.csv (列: date, label, category) を置くと、
    主要な時系列グラフに縦線とラベルで注釈が入る。ファイルが無ければ何もしない。
    category は "campaign" / "news" / "regulatory" を想定(色分け用。他の値は灰色)。
"""
import os
import sys
import glob
import logging
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows既定(cp932)での日本語出力の文字化け/エラーを防ぐ

# 日本語フォント設定。環境ごとに使えるフォント名が異なるため候補を並べ、
# 見つからないものは静かに無視する(警告ログだけ抑制)。
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
plt.rcParams["font.family"] = ["Yu Gothic", "Meiryo", "Hiragino Sans",
                                "IPAGothic", "IPAPGothic", "Noto Sans CJK JP",
                                "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False  # 一部の日本語フォントはマイナス記号を持たないため

from config import OUT_DIR, DUST_THRESHOLDS

EVENTS_PATH = "events.csv"
EVENT_CATEGORY_COLORS = {
    "campaign": "tab:orange",
    "news": "tab:purple",
    "regulatory": "tab:red",
}
EVENT_DEFAULT_COLOR = "gray"
CHAIN_NAMES = ["ethereum", "polygon", "avalanche", "kaia"]
SCOPE_LABELS = {
    "ethereum": "Ethereum", "polygon": "Polygon",
    "avalanche": "Avalanche", "kaia": "Kaia",
    "combined": "全チェーン合算",
}
BUCKET_LABELS = {
    "lt_1k": "1千円未満", "1k_10k": "1千円〜1万円", "10k_100k": "1万円〜10万円",
    "100k_1m": "10万円〜100万円", "ge_1m": "100万円以上",
}


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
    labels = [SCOPE_LABELS.get(c, c) for c in frames]

    # 9. チェーン別流通量(面積グラフ) — どのチェーンが伸びているかの比較
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.stackplot(idx, [supply[c] for c in frames], labels=labels, alpha=.85)
    ax.set_title("チェーン別 流通量の推移")
    ax.set_ylabel("流通量(JPYC)")
    ax.legend(loc="upper left"); ax.grid(alpha=.3)
    annotate_events(ax, events, idx.min(), idx.max())
    save(fig, "09_chain_supply_share.png", fig_dir)

    # 10. チェーン別ホルダー数(折れ線)
    fig, ax = plt.subplots(figsize=(10, 5))
    for c, lab in zip(frames, labels):
        ax.plot(idx, holders[c], label=lab, lw=1.5)
    ax.set_title("チェーン別 保有者数の推移")
    ax.set_ylabel("アドレス数(保有者数)")
    ax.legend(); ax.grid(alpha=.3)
    annotate_events(ax, events, idx.min(), idx.max())
    save(fig, "10_chain_holders.png", fig_dir)


def main():
    scope = pick_scope()
    scope_label = SCOPE_LABELS.get(scope, scope)
    fig_dir = os.path.join(OUT_DIR, f"figs_{scope}")
    os.makedirs(fig_dir, exist_ok=True)
    p = pd.read_csv(os.path.join(OUT_DIR, f"daily_panel_{scope}.csv"),
                    parse_dates=["date"])
    events = load_events()
    x_min, x_max = p.date.min(), p.date.max()

    # 1. ホルダー数(ダスト感度バンド) — ダスト保有者を除いた実質的な保有者数の分解
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(p.date, p.holders_gt0, label="残高 > 0円(全保有者)", lw=2)
    for t in DUST_THRESHOLDS:
        col = f"holders_ge{int(t)}"
        if col in p:
            ax.plot(p.date, p[col], label=f"残高 {int(t):,}円以上", lw=1.2)
    ax.set_title(f"保有者数の推移(残高閾値別) ― {scope_label}")
    ax.set_ylabel("アドレス数(保有者数)"); ax.legend(); ax.grid(alpha=.3)
    annotate_events(ax, events, x_min, x_max)
    save(fig, "01_holders_dust_sensitivity.png", fig_dir)

    # 2. 新規獲得 vs 離脱(7日移動平均)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(p.date, p.new_addresses.rolling(7).mean(), label="新規獲得(7日移動平均)")
    ax.plot(p.date, p.zeroed_addresses.rolling(7).mean(), label="離脱(7日移動平均)")
    ax.plot(p.date, p.resurrected_addresses.rolling(7).mean(),
            label="再保有(7日移動平均)", alpha=.7)
    ax.set_title(f"新規獲得アドレス数と離脱アドレス数の推移 ― {scope_label}")
    ax.set_ylabel("アドレス数/日"); ax.legend(); ax.grid(alpha=.3)
    annotate_events(ax, events, x_min, x_max)
    save(fig, "02_new_vs_churn.png", fig_dir)

    # 3. 保有分布(ホルダー数構成比)の推移
    bucket_cols = [c for c in p.columns if c.startswith("n_")]
    shares = p[bucket_cols].div(p["holders_gt0"], axis=0)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.stackplot(p.date, [shares[c] for c in bucket_cols],
                 labels=[BUCKET_LABELS.get(c[2:], c[2:]) for c in bucket_cols], alpha=.85)
    ax.set_title(f"保有額階層別 保有者構成比の推移 ― {scope_label}")
    ax.set_ylabel("保有者に占める割合"); ax.set_ylim(0, 1)
    ax.legend(loc="lower left", fontsize=8); ax.grid(alpha=.3)
    save(fig, "03_holder_buckets.png", fig_dir)

    # 4. 金額ベースの分布(どの層が価値を保有しているか)
    val_cols = [c for c in p.columns if c.startswith("val_")]
    vshares = p[val_cols].div(p["circulating_supply"], axis=0)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.stackplot(p.date, [vshares[c] for c in val_cols],
                 labels=[BUCKET_LABELS.get(c[4:], c[4:]) for c in val_cols], alpha=.85)
    ax.set_title(f"保有額階層別 流通量構成比の推移 ― {scope_label}")
    ax.set_ylabel("流通量に占める割合"); ax.set_ylim(0, 1)
    ax.legend(loc="lower left", fontsize=8); ax.grid(alpha=.3)
    save(fig, "04_value_buckets.png", fig_dir)

    # 5. 集中度
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(p.date, p.gini, label="Gini係数")
    ax.plot(p.date, p.top10_share, label="上位10アドレスの保有シェア")
    ax.plot(p.date, p.top100_share, label="上位100アドレスの保有シェア")
    ax.set_title(f"保有集中度の推移 ― {scope_label}"); ax.set_ylim(0, 1)
    ax.legend(); ax.grid(alpha=.3)
    annotate_events(ax, events, x_min, x_max)
    save(fig, "05_concentration.png", fig_dir)

    # 6. ミント(発行)・バーン(償却)・流通量 ― プロトコル上の生成/消滅そのもの
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(p.date, p.mint_volume.cumsum(), label="累積ミント量(新規発行)")
    ax.plot(p.date, p.burn_volume.cumsum(), label="累積バーン量(償却)")
    ax.plot(p.date, p.circulating_supply, label="流通量", lw=2)
    ax.set_title(f"ミント・バーン・流通量の累積推移 ― {scope_label}")
    ax.set_ylabel("JPYC"); ax.legend(); ax.grid(alpha=.3)
    annotate_events(ax, events, x_min, x_max)
    save(fig, "06_issuance_redemption.png", fig_dir)

    # 7. アクティビティ
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(p.date, p.transfers.rolling(7).mean(), label="送金件数(7日移動平均)")
    ax.plot(p.date, p.active_addresses.rolling(7).mean(),
            label="アクティブアドレス数(7日移動平均)")
    ax.set_title(f"アクティビティの推移 ― {scope_label}"); ax.legend(); ax.grid(alpha=.3)
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
        ax.set_xlabel("獲得からの経過週数")
        ax.set_title(f"獲得コホート別 保有継続率 ― {scope_label}")
        fig.colorbar(im, label="保有継続率")
        save(fig, "08_cohort_retention.png", fig_dir)

    # combinedの場合のみ: チェーン間比較図を追加
    if scope == "combined":
        plot_chain_comparison(fig_dir, events)

    # 11. 資金フローの内訳(発行/償還/ユーザー間取引) ― mint/burnとは別の視点。
    # 「発行」=運営ウォレットから一般アドレスへの送金(既に発行済みの供給が配られた分)
    # 「償還」=一般アドレスから運営ウォレットへの送金(既存トークンが運営に戻った分)
    # 「取引」=運営が一切関与しない、ユーザー同士の純粋な送金
    if {"issuance_volume", "redemption_volume", "transfer_volume"}.issubset(p.columns):
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(p.date, p.issuance_volume.rolling(7).mean(), label="発行(運営→ユーザー、7日移動平均)")
        ax.plot(p.date, p.redemption_volume.rolling(7).mean(), label="償還(ユーザー→運営、7日移動平均)")
        ax.plot(p.date, p.transfer_volume.rolling(7).mean(), label="ユーザー間取引(7日移動平均)")
        ax.set_title(f"資金フローの内訳の推移 ― {scope_label}")
        ax.set_ylabel("金額(JPYC/日)"); ax.legend(); ax.grid(alpha=.3)
        annotate_events(ax, events, x_min, x_max)
        save(fig, "11_flow_breakdown.png", fig_dir)

    # サマリーをテキストで
    last = p.iloc[-1]
    print(f"\n=== {scope} 最新日 ({last.date.date()}) ===")
    print(f"holders>0: {last.holders_gt0:,.0f} / >=1000円: {last.get('holders_ge1000', float('nan')):,.0f}")
    print(f"circulating: {last.circulating_supply:,.0f} JPYC")
    print(f"share of holders <10k yen: {last.share_holders_lt10k:.1%}")
    print(f"gini: {last.gini:.3f}  top10: {last.top10_share:.1%}")


if __name__ == "__main__":
    main()
