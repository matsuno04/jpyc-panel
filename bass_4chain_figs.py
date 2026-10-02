# -*- coding: utf-8 -*-
"""4チェーン(Ethereum/Polygon/Avalanche/Kaia)の Bass 当てはめを 1枚にまとめる。

analyze_bass.py の結果(output/bass_fit_results.csv, output/daily_panel_*.csv,
output/figs_bass/bass_curve_*.png)を再利用し、再フィットはしない。

出力:
  output/figs_bass/bass_4chain_panel.png    … 共通スタイルで描き直し + p/q/m/AIC 注記
  output/figs_bass/bass_4chain_montage.png  … 既存の個別図4枚(ロジスティック線つき)を2x2連結

使い方: python bass_4chain_figs.py   (先に analyze_bass.py を実行しておくこと)
"""
import os
import sys

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

plt.rcParams["font.family"] = ["Yu Gothic", "Meiryo", "Hiragino Sans",
                               "IPAGothic", "Noto Sans CJK JP", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False

OUT = "output"
FIGS = os.path.join(OUT, "figs_bass")

SCOPES = ["ethereum", "polygon", "avalanche", "kaia"]
LABELS = {"ethereum": "Ethereum", "polygon": "Polygon",
          "avalanche": "Avalanche", "kaia": "Kaia"}
# analyze_bass.py の T0_BY_SCOPE と一致させること
T0 = {"ethereum": "2025-10-27", "polygon": "2025-10-27",
      "avalanche": "2025-10-27", "kaia": "2026-05-15"}
COL = "cumulative_adopters_event_ex_hub"
SERIES = "event_ex_hub"


def bass_cum(t, p, q, m):
    ex = np.exp(-(p + q) * t)
    return m * (1 - ex) / (1 + (q / p) * ex)


def build_panel():
    fr = os.path.join(OUT, "bass_fit_results.csv")
    if not os.path.exists(fr):
        raise SystemExit("先に analyze_bass.py を実行してください (bass_fit_results.csv が無い)")
    fits = pd.read_csv(fr)
    fits = fits[fits["series"] == SERIES].set_index("scope")

    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    data_end = None
    for ax, sc in zip(axes.ravel(), SCOPES):
        d = pd.read_csv(os.path.join(OUT, f"daily_panel_{sc}.csv"))
        d["date"] = pd.to_datetime(d["date"])
        d["t"] = (d["date"] - pd.Timestamp(T0[sc])).dt.days
        d = d[(d["t"] >= 0) & d[COL].notna()].sort_values("t")
        t, N = d["t"].to_numpy(float), d[COL].to_numpy(float)
        data_end = max(data_end or d["date"].max(), d["date"].max())

        r = fits.loc[sc]
        p, q, m = float(r["p"]), float(r["q"]), float(r["m"])
        tg = np.linspace(0, t.max(), 400)
        tpeak = np.log(q / p) / (p + q) if p > 0 and q > 0 else np.nan

        ax.scatter(t, N, s=10, color="#4c78c8", alpha=0.55, label="実測値", zorder=2)
        ax.plot(tg, bass_cum(tg, p, q, m), color="#e4572e", lw=2.2,
                label="Bass 当てはめ", zorder=3)
        if 0 < tpeak < t.max():
            ax.axvline(tpeak, color="#888", ls=":", lw=1)
        better = "Bass" if float(r["aic_bass"]) < float(r["aic_logistic"]) else "ロジスティック"
        ax.set_title(
            f"{LABELS[sc]}   p={p:.4f}  q={q:.3f}  m={m:,.0f}\n"
            f"q/p={q/p:,.1f}   変曲 t≈{tpeak:.0f}日   AIC優位: {better}",
            fontsize=10.5)
        ax.set_xlabel(f"t(基準日 {T0[sc]} からの経過日数)", fontsize=9)
        ax.set_ylabel("累積採用者数(新規アドレス, ハブ除外)", fontsize=9)
        ax.legend(fontsize=8, loc="lower right")
        ax.grid(alpha=0.25)

    fig.suptitle(
        "JPYC 4チェーンの Bass 拡散モデル当てはめ  "
        f"(cumulative_adopters_event_ex_hub / パネル ~{data_end:%Y-%m-%d})",
        fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    p_out = os.path.join(FIGS, "bass_4chain_panel.png")
    fig.savefig(p_out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("saved", p_out)


def build_montage():
    try:
        from PIL import Image
    except ImportError:
        print("Pillow が無いため montage はスキップ (pip install pillow)")
        return
    files = [f"bass_curve_{s}.png" for s in SCOPES]
    paths = [os.path.join(FIGS, f) for f in files]
    if not all(os.path.exists(p) for p in paths):
        print("bass_curve_*.png が揃っていないため montage はスキップ")
        return
    ims = [Image.open(p).convert("RGB") for p in paths]
    w = max(i.width for i in ims)
    h = max(i.height for i in ims)
    ims = [i.resize((w, h)) if i.size != (w, h) else i for i in ims]
    canvas = Image.new("RGB", (w * 2, h * 2), "white")
    for k, im in enumerate(ims):
        canvas.paste(im, ((k % 2) * w, (k // 2) * h))
    p_out = os.path.join(FIGS, "bass_4chain_montage.png")
    canvas.save(p_out, dpi=(150, 150))
    print("saved", p_out, canvas.size)


if __name__ == "__main__":
    os.makedirs(FIGS, exist_ok=True)
    build_panel()
    build_montage()
