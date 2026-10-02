# -*- coding: utf-8 -*-
"""
analyze_bass.py — 各chainの採用曲線(cumulative_adopters系列)にBassモデルをフィットする。

対象: ethereum/polygon/avalanche/kaia(combinedは同一アドレスのチェーン間重複の
恐れがあるため除外)。主指標はcumulative_adopters_event_ex_hub(イベント単位、
DEXハブ除外)、感度分析用にcumulative_adopters_ex_hub(日次最終残高ベース)も
並行してフィットする。

使い方:
    python analyze_bass.py

出力:
    output/bass_fit_results.csv                      … scope×series の比較表
    output/bass_residuals_{scope}_{series}.csv        … 日次残差(実測-予測)
    output/figs_bass/bass_curve_{scope}.png           … 実測 vs Bass vs ロジスティック曲線(主指標)
    output/figs_bass/polygon_daily_new_adopters.png   … polygonの日次新規採用者数(1階差分)
"""
import os
import sys
import warnings
import logging

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit
import statsmodels.api as sm
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
plt.rcParams["font.family"] = ["Yu Gothic", "Meiryo", "Hiragino Sans",
                                "IPAGothic", "IPAPGothic", "Noto Sans CJK JP",
                                "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False

OUT_DIR = "output"
FIGS_DIR = os.path.join(OUT_DIR, "figs_bass")

SCOPE_LABELS = {"ethereum": "Ethereum", "polygon": "Polygon",
                "avalanche": "Avalanche", "kaia": "Kaia"}

# t=0(リベース日)。config.pyのLAUNCH_DATE(2025-10-01)とは異なり、
# JPYC社公式発表の正式ローンチ日(2025-10-27)を採用。kaiaのみ対応開始日。
T0_BY_SCOPE = {
    "ethereum": "2025-10-27",
    "polygon": "2025-10-27",
    "avalanche": "2025-10-27",
    "kaia": "2026-05-15",
}

SCOPES = ["ethereum", "polygon", "avalanche", "kaia"]

# (daily_panel列名, レポート用の短いseries名)
SERIES = [
    ("cumulative_adopters_event_ex_hub", "event_ex_hub"),      # 主指標(B案・イベント単位)
    ("cumulative_adopters_ex_hub", "snapshot_ex_hub"),          # 感度分析用(A案・日次最終残高)
]


# ---------------------------------------------------------------- 1. Bassモデル
def bass_cumulative(t, p, q, m):
    """Bass拡散モデルの累積採用者数(閉形式)。
    N(t) = m * (1 - exp(-(p+q)t)) / (1 + (q/p)*exp(-(p+q)t))
    """
    t = np.asarray(t, dtype=float)
    ex = np.exp(-(p + q) * t)
    return m * (1 - ex) / (1 + (q / p) * ex)


# ---------------------------------------------------------------- 2. 多点リスタートcurve_fit
def fit_bass_robust(t, N, n_restarts=10, init_guess=None, seed=0):
    """複数の初期値からcurve_fitをリスタートし、残差平方和(SSE)最小の解を採用する。
    局所解に落ちるリスクを避けるため。bounds: p∈[1e-6,1.0], q∈[1e-6,2.0],
    m∈[N[-1], N[-1]*50]。"""
    t = np.asarray(t, dtype=float)
    N = np.asarray(N, dtype=float)
    n_final = N[-1]
    lo = [1e-6, 1e-6, max(n_final, 1.0)]
    hi = [1.0, 2.0, max(n_final * 50, n_final + 1.0)]

    rng = np.random.default_rng(seed)
    guesses = []
    if init_guess is not None:
        guesses.append(np.clip(init_guess, lo, hi))
    for _ in range(n_restarts):
        p0 = rng.uniform(1e-4, 0.05)
        q0 = rng.uniform(0.01, 0.8)
        m0 = rng.uniform(n_final * 1.01, n_final * 5)
        guesses.append([p0, q0, m0])

    best_popt, best_pcov, best_sse = None, None, np.inf
    for g in guesses:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                popt, pcov = curve_fit(
                    bass_cumulative, t, N, p0=g, bounds=(lo, hi), maxfev=20000
                )
            resid = N - bass_cumulative(t, *popt)
            sse = float(np.sum(resid ** 2))
            if sse < best_sse:
                best_sse, best_popt, best_pcov = sse, popt, pcov
        except (RuntimeError, ValueError):
            continue

    if best_popt is None:
        raise RuntimeError("Bassモデルのフィットに全リスタートで失敗した")
    return best_popt, best_pcov, best_sse


# ---------------------------------------------------------------- 3. ブロックブートストラップ
def bootstrap_ci(t, N, base_popt=None, n_boot=200, block_len=None, seed=0):
    """残差のサーキュラーブロックブートストラップでp,q,mの95%信頼区間を算出。
    時系列の自己相関を保つため、単純なiidリサンプリングではなくブロック単位で
    リサンプルする。p-q間の相関係数も併せて返す。"""
    t = np.asarray(t, dtype=float)
    N = np.asarray(N, dtype=float)
    n = len(N)

    if base_popt is None:
        base_popt, _, _ = fit_bass_robust(t, N)
    y_hat = bass_cumulative(t, *base_popt)
    resid = N - y_hat

    if block_len is None:
        block_len = max(5, int(round(n ** (1 / 3) * 3)))
    block_len = min(block_len, n)
    n_blocks = int(np.ceil(n / block_len))

    rng = np.random.default_rng(seed)
    boot_params = []
    for _ in range(n_boot):
        starts = rng.integers(0, n, size=n_blocks)
        resid_boot = np.concatenate(
            [resid[[(s + i) % n for i in range(block_len)]] for s in starts]
        )[:n]
        N_boot = y_hat + resid_boot
        N_boot = np.maximum.accumulate(N_boot)  # 累積量としての単調性を保つ
        try:
            popt_b, _, _ = fit_bass_robust(t, N_boot, n_restarts=3, init_guess=base_popt, seed=rng.integers(1 << 30))
            boot_params.append(popt_b)
        except RuntimeError:
            continue

    if len(boot_params) < 10:
        return {"p_ci": (np.nan, np.nan), "q_ci": (np.nan, np.nan),
                "m_ci": (np.nan, np.nan), "p_q_correlation": np.nan,
                "n_boot_success": len(boot_params)}

    arr = np.array(boot_params)  # shape (n_success, 3)
    p_ci = tuple(np.percentile(arr[:, 0], [2.5, 97.5]))
    q_ci = tuple(np.percentile(arr[:, 1], [2.5, 97.5]))
    m_ci = tuple(np.percentile(arr[:, 2], [2.5, 97.5]))
    p_q_corr = float(np.corrcoef(arr[:, 0], arr[:, 1])[0, 1])
    return {"p_ci": p_ci, "q_ci": q_ci, "m_ci": m_ci,
            "p_q_correlation": p_q_corr, "n_boot_success": len(boot_params)}


# ---------------------------------------------------------------- 4. 離散OLS推定(感度分析)
def fit_bass_discrete(N):
    """Srinivasan & Mason (1986) 方式の離散時間OLS推定。
    S_t = a + b*N_{t-1} + c*N_{t-1}^2  (S_t = N_t - N_{t-1})
    a=p*m, b=q-p, c=-q/m の関係からp,q,mを逆算する。連続時間の非線形フィット
    (fit_bass_robust)との一致度を見る感度分析用。"""
    N = np.asarray(N, dtype=float)
    if len(N) < 5:
        return None
    N_prev = N[:-1]
    S = np.diff(N)
    X = sm.add_constant(np.column_stack([N_prev, N_prev ** 2]))
    model = sm.OLS(S, X).fit()
    a, b, c = model.params
    if c == 0:
        return None
    disc = b ** 2 - 4 * a * c
    if disc < 0:
        return None
    sqrt_disc = np.sqrt(disc)
    m_candidates = [(-b + sqrt_disc) / (2 * c), (-b - sqrt_disc) / (2 * c)]
    plausible = [m for m in m_candidates if m > N[-1]]
    m = min(plausible) if plausible else max(m_candidates)
    if m <= 0:
        return None
    p = a / m
    q = -c * m
    return {"p": p, "q": q, "m": m, "ols_model": model}


# ---------------------------------------------------------------- 5. ロジスティック曲線とのAIC比較
def _logistic(t, m, k, t_mid):
    return m / (1 + np.exp(-k * (np.asarray(t, dtype=float) - t_mid)))


def _aic(sse, n, n_params):
    """最小二乗回帰の標準的なAIC(ガウス誤差を仮定)。"""
    if sse <= 0 or n <= 0:
        return np.nan
    return n * np.log(sse / n) + 2 * n_params


def _fit_logistic(t, N):
    """ロジスティック曲線(3パラメータ)をフィットする。失敗時はNoneを返す。"""
    t = np.asarray(t, dtype=float)
    N = np.asarray(N, dtype=float)
    n_final = N[-1]
    lo = [n_final, 1e-6, t.min() - (t.max() - t.min())]
    hi = [n_final * 50, 2.0, t.max() + (t.max() - t.min())]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            popt_log, _ = curve_fit(
                _logistic, t, N,
                p0=[n_final * 2, 0.02, float(np.median(t))],
                bounds=(lo, hi), maxfev=20000,
            )
        return popt_log
    except (RuntimeError, ValueError):
        return None


def compare_models(t, N, bass_popt=None):
    """Bassモデルとロジスティック曲線(3パラメータ)をAICで比較する。"""
    t = np.asarray(t, dtype=float)
    N = np.asarray(N, dtype=float)
    n = len(N)

    if bass_popt is None:
        bass_popt, _, sse_bass = fit_bass_robust(t, N)
    else:
        sse_bass = float(np.sum((N - bass_cumulative(t, *bass_popt)) ** 2))
    aic_bass = _aic(sse_bass, n, 3)

    popt_log = _fit_logistic(t, N)
    if popt_log is not None:
        sse_log = float(np.sum((N - _logistic(t, *popt_log)) ** 2))
        aic_log = _aic(sse_log, n, 3)
    else:
        aic_log = np.nan

    return aic_bass, aic_log


# ---------------------------------------------------------------- 6. 残差計算
def compute_residuals(t, N, dates, popt):
    """実測値と予測値の日次残差を返す。"""
    N_pred = bass_cumulative(t, *popt)
    return pd.DataFrame({
        "date": dates,
        "t": t,
        "N_actual": N,
        "N_pred": N_pred,
        "residual": np.asarray(N, dtype=float) - N_pred,
    })


# ---------------------------------------------------------------- プロット
def plot_bass_curve(scope, t, N, bass_popt, logistic_popt, out_path):
    """実測値・Bassフィット・ロジスティックフィットを重ねてプロットする。
    t=0付近の凹凸(加速/減速)を確認しやすいよう、t軸の原点を含めて描画する。"""
    t = np.asarray(t, dtype=float)
    N = np.asarray(N, dtype=float)
    t_fine = np.linspace(t.min(), t.max(), 400)

    fig, ax = plt.subplots(figsize=(9, 5.5))
    ax.scatter(t, N, s=10, color="tab:blue", alpha=0.5, label="実測値", zorder=3)
    ax.plot(t_fine, bass_cumulative(t_fine, *bass_popt), color="tab:red", linewidth=2,
            label=f"Bass (p={bass_popt[0]:.4f}, q={bass_popt[1]:.4f})", zorder=4)
    if logistic_popt is not None:
        ax.plot(t_fine, _logistic(t_fine, *logistic_popt), color="tab:green", linewidth=2,
                linestyle="--", label="ロジスティック", zorder=4)
    ax.axvline(0, color="gray", linewidth=1, linestyle=":", alpha=0.7)
    ax.set_xlabel("t(リベース日からの経過日数)")
    ax.set_ylabel("累積採用者数(cumulative_adopters_event_ex_hub)")
    ax.set_title(f"{SCOPE_LABELS.get(scope, scope)} — 採用曲線フィット")
    ax.legend(loc="lower right", fontsize=9)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_daily_new_adopters(scope, dates, N, out_path):
    """日次新規採用者数(1階差分)の推移をプロットする(生値+7日移動平均)。
    単調減少が続いていれば「最初から減速している」仮説の裏付けとなる。"""
    dates = pd.to_datetime(pd.Series(dates)).reset_index(drop=True)
    N = np.asarray(N, dtype=float)
    diffs = np.diff(N)
    plot_dates = dates.iloc[1:].reset_index(drop=True)
    rolling7 = pd.Series(diffs).rolling(7, min_periods=1).mean()

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(plot_dates, diffs, color="tab:blue", linewidth=0.8, alpha=0.4, label="日次(生値)")
    ax.plot(plot_dates, rolling7, color="tab:red", linewidth=2, label="7日移動平均")
    ax.axhline(0, color="gray", linewidth=1)
    ax.set_xlabel("日付")
    ax.set_ylabel("日次新規採用者数(1階差分)")
    ax.set_title(f"{SCOPE_LABELS.get(scope, scope)} — 日次新規採用者数の推移")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------- メイン処理
def fmt_ci(ci):
    lo, hi = ci
    if np.isnan(lo) or np.isnan(hi):
        return "[NA, NA]"
    return f"[{lo:.4g}, {hi:.4g}]"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(FIGS_DIR, exist_ok=True)
    results = []

    for scope in SCOPES:
        panel_path = os.path.join(OUT_DIR, f"daily_panel_{scope}.csv")
        if not os.path.exists(panel_path):
            print(f"[{scope}] スキップ: {panel_path} が無い")
            continue
        df = pd.read_csv(panel_path)
        df["date"] = pd.to_datetime(df["date"])
        t0 = pd.Timestamp(T0_BY_SCOPE[scope])

        for col, series_name in SERIES:
            if col not in df.columns:
                print(f"[{scope}/{series_name}] スキップ: 列 {col} が無い")
                continue
            sub = df[["date", col]].copy()
            sub["t"] = (sub["date"] - t0).dt.days
            sub = sub[sub["t"] >= 0].sort_values("t").reset_index(drop=True)
            if len(sub) < 10:
                print(f"[{scope}/{series_name}] スキップ: t>=0のデータが{len(sub)}件しかない")
                continue

            t = sub["t"].to_numpy(dtype=float)
            N = sub[col].to_numpy(dtype=float)
            dates = sub["date"]

            try:
                popt, pcov, sse = fit_bass_robust(t, N, n_restarts=10)
            except RuntimeError as e:
                print(f"[{scope}/{series_name}] フィット失敗: {e}")
                continue
            p, q, m = popt

            ci = bootstrap_ci(t, N, base_popt=popt, n_boot=200)
            aic_bass, aic_logistic = compare_models(t, N, bass_popt=popt)

            inflection_t = float(np.log(q / p) / (p + q)) if p > 0 and q > 0 else np.nan
            q_over_p = float(q / p) if p > 0 else np.nan

            results.append({
                "scope": scope, "series": series_name, "n_obs": len(sub),
                "p": p, "q": q, "m": m,
                "p_ci": fmt_ci(ci["p_ci"]), "q_ci": fmt_ci(ci["q_ci"]), "m_ci": fmt_ci(ci["m_ci"]),
                "p_q_correlation": ci["p_q_correlation"],
                "q_over_p": q_over_p, "inflection_t": inflection_t,
                "aic_bass": aic_bass, "aic_logistic": aic_logistic,
            })

            resid_df = compute_residuals(t, N, dates, popt)
            resid_df.to_csv(
                os.path.join(OUT_DIR, f"bass_residuals_{scope}_{series_name}.csv"), index=False
            )

            # 主指標(event_ex_hub)のみ: 実測 vs Bass vs ロジスティックの重ね描き
            if series_name == "event_ex_hub":
                popt_log = _fit_logistic(t, N)
                plot_bass_curve(scope, t, N, popt, popt_log,
                                 os.path.join(FIGS_DIR, f"bass_curve_{scope}.png"))
                if scope == "polygon":
                    plot_daily_new_adopters(
                        scope, dates, N,
                        os.path.join(FIGS_DIR, "polygon_daily_new_adopters.png")
                    )

            # 離散OLS推定(感度分析用、CSVには含めずコンソールで連続時間フィットと比較)
            disc = fit_bass_discrete(N)
            if disc:
                print(f"[{scope}/{series_name}] 連続時間: p={p:.5f} q={q:.4f} m={m:,.0f}  |  "
                      f"離散OLS: p={disc['p']:.5f} q={disc['q']:.4f} m={disc['m']:,.0f}  "
                      f"(bootstrap成功{ci['n_boot_success']}/200)")
            else:
                print(f"[{scope}/{series_name}] 連続時間: p={p:.5f} q={q:.4f} m={m:,.0f}  |  "
                      f"離散OLS: 推定不能  (bootstrap成功{ci['n_boot_success']}/200)")

    res_df = pd.DataFrame(results)
    res_df.to_csv(os.path.join(OUT_DIR, "bass_fit_results.csv"), index=False)
    print(f"\n保存: {os.path.join(OUT_DIR, 'bass_fit_results.csv')} ({len(res_df)}行)")


if __name__ == "__main__":
    main()
