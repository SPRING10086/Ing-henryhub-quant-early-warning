# -*- coding: utf-8 -*-
"""
P4 · 风控评估（可复现，供审核员独立复算）。

只读 P3 产物，不覆盖任何 raw / P3 文件。输出（新增文件）：
- data/processed/risk_eval_weekly.csv   # 逐周可复算中间表
- data/processed/risk_eval_summary.json # 全部汇总指标（机器可读）

评估内容：
1) 全周期方向命中率 + ROC-AUC（v1/logit/lgbm，OOS 全周期，不挑区间）；
2) 分情景命中率（采暖季 11-3 月 / 非采暖季 4-10 月 / 2022 欧洲能源危机 /
   Freeport LNG 出口扰动期），Wilson 95% 置信区间，小样本标注；
3) 双基准：随机 50%（二项检验）与朴素惯性外推（真值 lag1 方向），McNemar 配对检验；
4) v1 权重 sd=0/hdd=0 判定：首期训练窗覆盖/单因子信号/网格搜索地貌 + 跨窗口稳定性；
5) 失效案例复盘数据（逐周命中 + 分折命中率）；
6) 风险标签规则触发周数统计（寒潮持续 / LNG 出口扰动 / 欧盟储气低位 / 高波动区间）。

红线：无价格点位、无交易建议、只报 OOS 全周期/分情景。
"""
from __future__ import annotations

import json
import math
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.metrics import accuracy_score, roc_auc_score

ROOT = Path(__file__).resolve().parents[2]
PROC = ROOT / "data" / "processed"
DOCS = ROOT / "docs"

FEATURES = ["sd_feature", "hdd_anomaly_4wk", "supply_shock_4wk_z", "eu_storage_deviation"]
SIGNS = {"sd_feature": -1.0, "hdd_anomaly_4wk": 1.0, "supply_shock_4wk_z": -1.0, "eu_storage_deviation": -1.0}
SIGN_VEC = np.array([SIGNS[f] for f in FEATURES])
WEIGHT_GRID = [0.0, 0.25, 0.5, 0.75, 1.0]
FEAT_CN = {"sd_feature": "库存偏离度SD", "hdd_anomaly_4wk": "HDD4周异常",
           "supply_shock_4wk_z": "供给冲击Z", "eu_storage_deviation": "欧盟储气偏离"}


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return ((center - half) / denom, (center + half) / denom)


def mcnemar(model_pred: np.ndarray, base_pred: np.ndarray, y: np.ndarray):
    """McNemar 精确检验（二项），比较 model 与 base 的配对命中差异。

    参数为「预测方向」0/1，内部自行计算命中；model_pred / base_pred / y 逐元素对齐。
    """
    m = np.asarray(model_pred, dtype=bool)
    b = np.asarray(base_pred, dtype=bool)
    yy = np.asarray(y, dtype=bool)
    m_correct = (m == yy)
    b_correct = (b == yy)
    b_disc = int((m_correct & ~b_correct).sum())   # model 对、base 错
    c_disc = int((~m_correct & b_correct).sum())   # model 错、base 对
    n_disc = b_disc + c_disc
    p = stats.binomtest(b_disc, n_disc, 0.5).pvalue if n_disc > 0 else 1.0
    return {"model_only_correct": b_disc, "base_only_correct": c_disc,
            "discordant": n_disc, "p_value": float(p)}


def v1_grid_landscape(X: pd.DataFrame, y: pd.Series):
    """复现 P3 v1 网格搜索：返回 (结果列表, 最优)。"""
    X = X[FEATURES].reset_index(drop=True).astype(float)
    y = np.asarray(y, dtype=float)
    mu = X.mean()
    sigma = X.std(ddof=0).clip(lower=1e-9)
    Xz = np.nan_to_num((X - mu) / sigma.replace(0, np.nan), nan=0.0)
    n = len(X)
    results = []
    for w in product(WEIGHT_GRID, repeat=4):
        if sum(w) == 0:
            continue
        wn = np.array(w) / sum(w) * SIGN_VEC
        s = Xz @ wn
        p = (s > 0).astype(int)
        accs = []
        for vs in range(n // 2, n, 26):
            if vs + 26 > n:
                vs = max(n // 2, n - 26)
            accs.append(accuracy_score(y[vs:vs + 26], p[vs:vs + 26]))
            if vs + 26 >= n:
                break
        acc = float(np.mean(accs)) if accs else float(accuracy_score(y, p))
        results.append({"acc": acc, "w_norm": [round(x / sum(w), 4) for x in w]})
    best = max(results, key=lambda r: r["acc"])
    return results, best


def main() -> None:
    pred = pd.read_csv(PROC / "backtest_predictions.csv", parse_dates=["week_anchor"])
    feat = pd.read_csv(PROC / "features_weekly.csv", parse_dates=["week_anchor"])
    cuts = pd.read_csv(PROC / "walkforward_train_cutoffs.csv",
                       parse_dates=["train_start", "train_end", "test_start", "test_end"])
    master = pd.read_csv(PROC / "weekly_master.csv", parse_dates=["week_anchor"])

    # ---- 方向 ----
    pred["logit_dir"] = (pred["logit_p_up"] > 0.5).astype(int)
    pred["lgbm_dir"] = (pred["lgbm_p_up"] > 0.5).astype(int)
    pred["v1_dir"] = pred["v1_direction"].astype(int)

    # ---- 朴素惯性外推（用上周已实现方向预测下周，无未来信息）----
    hh = feat[["week_anchor", "henry_hub_avg"]].sort_values("week_anchor").reset_index(drop=True)
    hh["prev_hh"] = hh["henry_hub_avg"].shift(1)
    hh["inertia_dir"] = (hh["henry_hub_avg"] > hh["prev_hh"]).astype(int)
    pred = pred.merge(hh[["week_anchor", "inertia_dir"]], on="week_anchor", how="left")
    pred["inertia_dir"] = pred["inertia_dir"].fillna(0).astype(int)

    y = pred["y_true_h1"].astype(int).values
    models = {"v1": "v1_dir", "logit": "logit_dir", "lgbm": "lgbm_dir", "inertia": "inertia_dir"}
    acc = {m: float(accuracy_score(y, pred[c].values)) for m, c in models.items()}
    auc = {
        "v1": float(roc_auc_score(y, pred["v1_p_up"].values)),
        "logit": float(roc_auc_score(y, pred["logit_p_up"].values)),
        "lgbm": float(roc_auc_score(y, pred["lgbm_p_up"].values)),
        "inertia": float(roc_auc_score(y, pred["inertia_dir"].values)),
    }

    # ---- 随机 50% 基准（二项检验）----
    n = len(y)
    binomial = {}
    for m, c in models.items():
        k = int((pred[c].values == y).sum())
        binomial[m] = {"hits": k, "n": n, "p_two_sided": float(stats.binomtest(k, n, 0.5).pvalue)}

    # ---- McNemar：模型 vs 惯性 ----
    mcn = {}
    for m in ["v1", "logit", "lgbm"]:
        mcn[m] = mcnemar(pred[models[m]].values, pred["inertia_dir"].values, y)

    # ---- 分情景 ----
    mo = pred["week_anchor"].dt.month
    pred["is_heating"] = mo.isin([11, 12, 1, 2, 3]).astype(int)
    pred["is_non_heating"] = (~mo.isin([11, 12, 1, 2, 3])).astype(int)
    pred["is_shock_2022"] = ((pred["week_anchor"] >= "2022-02-24") & (pred["week_anchor"] <= "2023-03-31")).astype(int)
    pred["is_shock_freeport"] = ((pred["week_anchor"] >= "2022-06-08") & (pred["week_anchor"] <= "2023-02-28")).astype(int)

    scenario_cols = {"is_heating": "采暖季(11-3月)", "is_non_heating": "非采暖季(4-10月)",
                     "is_shock_2022": "外部冲击:2022欧洲能源危机", "is_shock_freeport": "外部冲击:Freeport LNG扰动"}
    scenario = {}
    for col, label in scenario_cols.items():
        mask = pred[col].astype(bool)
        sub = pred[mask]
        scenario[label] = {"n": int(mask.sum())}
        for m, c in models.items():
            k = int((sub[c].values == sub["y_true_h1"].values).sum())
            ci = wilson_ci(k, len(sub))
            scenario[label][m] = {"hits": k, "acc": round(k / len(sub), 4) if len(sub) else None,
                                  "wilson_lo": round(ci[0], 4), "wilson_hi": round(ci[1], 4)}

    # ---- 分折命中率（失效案例用）----
    fold_rows = []
    for _, c in cuts.iterrows():
        sub = pred[pred["fold"] == c["fold"]]
        rec = {"fold": int(c["fold"]), "test_start": str(c["test_start"].date()),
               "test_end": str(c["test_end"].date()), "n": int(len(sub))}
        for m, col in models.items():
            rec[m] = round(float(accuracy_score(sub["y_true_h1"], sub[col])), 4)
        fold_rows.append(rec)

    # ---- v1 权重复核 ----
    ok = feat.dropna(subset=FEATURES + ["label_h1"]).sort_values("week_anchor").reset_index(drop=True)
    first = int(ok[ok.week_anchor >= "2019-01-04"].index[0])
    tr0 = ok.iloc[first:first + 156]
    coverage = {f: int(tr0[f].notna().sum()) for f in FEATURES}
    corr = {}
    single_feat = {}
    for f in FEATURES:
        sub = tr0[[f, "label_h1"]].dropna()
        corr[f] = {"pearson": round(float(sub[f].corr(sub["label_h1"])), 4),
                   "n": int(len(sub))}
        x = tr0[f].astype(float).fillna(0.0)
        p_single = ((SIGNS[f] * x) > 0).astype(int)
        single_feat[f] = round(float(accuracy_score(tr0["label_h1"].values, p_single.values)), 4)

    results0, best0 = v1_grid_landscape(tr0[FEATURES], tr0["label_h1"])
    flat_1pp = sum(1 for r in results0 if best0["acc"] - r["acc"] <= 0.01)
    flat_05pp = sum(1 for r in results0 if best0["acc"] - r["acc"] <= 0.005)

    # 跨窗口稳定性（前 6 个训练窗复跑网格搜索）
    stability = []
    for k in range(6):
        a = first + k * 13
        tr = ok.iloc[a:a + 156]
        res, best = v1_grid_landscape(tr[FEATURES], tr["label_h1"])
        single = {}
        for f in FEATURES:
            x = tr[f].astype(float).fillna(0.0)
            p_single = ((SIGNS[f] * x) > 0).astype(int)
            single[f] = round(float(accuracy_score(tr["label_h1"].values, p_single.values)), 4)
        stability.append({"window": k, "train_end": str(tr["week_anchor"].iloc[-1].date()),
                          "best_w_norm": best["w_norm"], "best_acc": round(best["acc"], 4),
                          "single_feat_acc": single,
                          "n_flat_1pp": sum(1 for r in res if best["acc"] - r["acc"] <= 0.01)})

    # ---- 风险标签 ----
    feat = feat.sort_values("week_anchor").reset_index(drop=True)
    feat["hdd_anom_z"] = (feat["hdd_anomaly_4wk"] - feat["hdd_anomaly_4wk"].mean()) / feat["hdd_anomaly_4wk"].std()
    feat["eu_dev_z"] = (feat["eu_storage_deviation"] - feat["eu_storage_deviation"].mean()) / feat["eu_storage_deviation"].std()

    master = master.sort_values("week_anchor").reset_index(drop=True)
    master["hh_ret"] = master["henry_hub_avg"].pct_change()
    master["hh_vol8"] = master["hh_ret"].rolling(8).std()
    master["lng_z52"] = ((master["lng_export_mmcf"] - master["lng_export_mmcf"].rolling(52).mean())
                         / master["lng_export_mmcf"].rolling(52).std().replace(0, np.nan))

    vol_p75 = master["hh_vol8"].quantile(0.75)
    vol_p95 = master["hh_vol8"].quantile(0.95)

    risk_src = feat[["week_anchor", "hdd_anom_z", "eu_dev_z"]].merge(
        master[["week_anchor", "lng_z52", "hh_vol8"]], on="week_anchor", how="left")
    pred = pred.merge(risk_src, on="week_anchor", how="left")

    pred["flag_cold_y"] = (pred["hdd_anom_z"] >= 1.0).astype(int)
    pred["flag_cold_r"] = (pred["hdd_anom_z"] >= 2.0).astype(int)
    pred["flag_lng_y"] = (pred["lng_z52"] <= -1.0).astype(int)
    pred["flag_lng_r"] = (pred["lng_z52"] <= -2.0).astype(int)
    pred["flag_eu_y"] = (pred["eu_dev_z"] <= -1.0).astype(int)
    pred["flag_eu_r"] = (pred["eu_dev_z"] <= -2.0).astype(int)
    pred["flag_vol_y"] = (pred["hh_vol8"] >= vol_p75).astype(int)
    pred["flag_vol_r"] = (pred["hh_vol8"] >= vol_p95).astype(int)

    risk_labels = [
        ("flag_cold_y", "寒潮持续-黄"), ("flag_cold_r", "寒潮持续-红"),
        ("flag_lng_y", "LNG出口扰动-黄"), ("flag_lng_r", "LNG出口扰动-红"),
        ("flag_eu_y", "欧盟储气低位-黄"), ("flag_eu_r", "欧盟储气低位-红"),
        ("flag_vol_y", "高波动-黄"), ("flag_vol_r", "高波动-红"),
    ]
    risk_stats = {}
    for col, label in risk_labels:
        mask = pred[col].astype(bool)
        sub = pred[mask]
        risk_stats[label] = {
            "n_trigger": int(mask.sum()),
            "v1_acc": round(float(accuracy_score(sub["y_true_h1"], sub["v1_dir"])), 4) if len(sub) else None,
            "logit_acc": round(float(accuracy_score(sub["y_true_h1"], sub["logit_dir"])), 4) if len(sub) else None,
            "lgbm_acc": round(float(accuracy_score(sub["y_true_h1"], sub["lgbm_dir"])), 4) if len(sub) else None,
        }

    # ---- 中间表落盘 ----
    out_cols = ["fold", "week_anchor", "y_true_h1",
                "v1_dir", "v1_p_up", "logit_dir", "logit_p_up", "lgbm_dir", "lgbm_p_up",
                "inertia_dir", "is_heating", "is_non_heating", "is_shock_2022", "is_shock_freeport",
                "hdd_anom_z", "eu_dev_z", "lng_z52", "hh_vol8",
                "flag_cold_y", "flag_cold_r", "flag_lng_y", "flag_lng_r",
                "flag_eu_y", "flag_eu_r", "flag_vol_y", "flag_vol_r"]
    pred[out_cols].to_csv(PROC / "risk_eval_weekly.csv", index=False)

    summary = {
        "n_weeks": n,
        "n_folds": int(cuts["fold"].nunique()),
        "test_span": [str(pred["week_anchor"].min().date()), str(pred["week_anchor"].max().date())],
        "accuracy": acc,
        "auc": auc,
        "binomial_vs_random50": binomial,
        "mcnemar_vs_inertia": mcn,
        "scenario": scenario,
        "fold_accuracy": fold_rows,
        "v1_weights_best_first_window": best0["w_norm"],
        "v1_grid": {"n_combos": len(results0), "best_acc": round(best0["acc"], 4),
                    "n_flat_1pp": flat_1pp, "n_flat_05pp": flat_05pp},
        "v1_first_window": {"coverage_nonnull": coverage, "corr_with_label": corr,
                            "single_feat_sign_acc": single_feat},
        "v1_stability_windows": stability,
        "risk_labels": {"vol_p75": round(float(vol_p75), 6), "vol_p95": round(float(vol_p95), 6),
                        "stats": risk_stats},
        "redline": "无点位、无交易建议、只报OOS全周期/分情景、个人研究不构成交易建议",
    }

    with open(PROC / "risk_eval_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=float)

    # ---- 控制台打印 ----
    print("== P4 风控评估汇总 ==")
    print(f"OOS 周数={n}  折数={cuts['fold'].nunique()}  区间={summary['test_span']}")
    print("方向命中率:", {k: round(v, 4) for k, v in acc.items()})
    print("ROC-AUC:", {k: round(v, 4) for k, v in auc.items()})
    print("二项检验(模型 vs 随机50%):", {k: round(v['p_two_sided'], 4) for k, v in binomial.items()})
    print("McNemar(模型 vs 惯性):", {k: v for k, v in mcn.items()})
    print("v1 首期最优权重:", best0["w_norm"], " 首期内部CV acc=", round(best0["acc"], 4),
          " 1pp平坦组合数=", flat_1pp)
    print("v1 首期单因子方向命中率:", single_feat)
    print("v1 首期特征与label相关系数:", corr)
    for s in stability:
        print("  窗口", s["window"], s["train_end"], "best_w=", s["best_w_norm"],
              "best_acc=", s["best_acc"], "single=", s["single_feat_acc"])
    print("风险标签触发周数:", {k: v["n_trigger"] for k, v in risk_stats.items()})


if __name__ == "__main__":
    main()
