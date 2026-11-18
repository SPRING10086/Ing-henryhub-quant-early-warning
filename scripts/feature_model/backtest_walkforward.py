# -*- coding: utf-8 -*-
"""
P3 · Walk-Forward 滚动回测（防未来信息）—— 编排 v1 规则 + v2 概率双模型。

- 滚动训练窗 156 周（3 年），步长 13 周，每期外推 13 周；
- 首期训练 2019-01-04 起，测试 2022Q1 起，滚动至数据末尾；
- v1 权重仅在首期训练窗内网格搜索（expanding 时序 CV），确定后全程固定；
  z-score 标准化每折用当折训练集拟合；
- 只输出方向概率 P(up) 与松紧等级，不输出价格点位。

输出（data/processed/、docs/）：
- backtest_predictions.csv
- walkforward_train_cutoffs.csv
- docs/feature_importance.png
- docs/backtest_report.md
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parent))

import model_v1_rules as m1
import model_v2_prob as m2

ROOT = Path(__file__).resolve().parents[2]
PROC = ROOT / "data" / "processed"
DOCS = ROOT / "docs"

TRAIN_WEEKS = 156
TEST_WEEKS = 13
STEP_WEEKS = 13
FIRST_TRAIN_DATE = "2019-01-04"


def main():
    f = pd.read_csv(PROC / "features_weekly.csv", parse_dates=["week_anchor"])
    f = f.sort_values("week_anchor").reset_index(drop=True)

    FEATURES = m1.FEATURES
    ok = f.dropna(subset=FEATURES + ["label_h1"]).reset_index(drop=True)

    first = int(ok[ok.week_anchor >= FIRST_TRAIN_DATE].index[0])
    folds = []
    i = first
    while i + TRAIN_WEEKS + TEST_WEEKS <= len(ok):
        folds.append((i, i + TRAIN_WEEKS, i + TRAIN_WEEKS + TEST_WEEKS))
        i += STEP_WEEKS

    # v1 权重：仅在首期训练窗网格搜索，之后全程固定
    a0, b0, c0 = folds[0]
    v1_search = m1.V1Rules().fit(ok.iloc[a0:b0][FEATURES], ok.iloc[a0:b0]["label_h1"])
    best_weights = v1_search.weights.copy()

    rows, cutoffs = [], []
    for k, (a, b, c) in enumerate(folds):
        trX = ok.iloc[a:b][FEATURES]
        trY = ok.iloc[a:b]["label_h1"]
        teX = ok.iloc[b:c][FEATURES]
        teY = ok.iloc[b:c]["label_h1"]
        teW = ok.iloc[b:c]["week_anchor"]
        train_start = ok.iloc[a]["week_anchor"].date()
        train_end = ok.iloc[b - 1]["week_anchor"].date()
        test_start = ok.iloc[b]["week_anchor"].date()
        test_end = ok.iloc[c - 1]["week_anchor"].date()

        # v1：固定权重，每折独立拟合 scaler
        v1 = m1.V1Rules()
        v1.weights = best_weights
        v1.fit_scaler(trX)
        v1_dir = v1.direction(teX)
        v1_cls = v1.classify(teX)
        v1_p = v1.p_up(teX)

        # v2：Logistic 主基线 + LightGBM 对比
        logit = m2.fit_logistic(trX, trY)
        lgbm = m2.fit_lightgbm(trX, trY)
        logit_p = m2.predict_proba(logit, teX)
        lgbm_p = m2.predict_proba(lgbm, teX)

        cutoffs.append({
            "fold": k, "train_start": train_start, "train_end": train_end,
            "test_start": test_start, "test_end": test_end,
            "n_train": b - a, "n_test": c - b,
        })
        for j in range(len(teX)):
            rows.append({
                "fold": k, "week_anchor": teW.iloc[j].date(),
                "train_start": train_start, "train_end": train_end,
                "y_true_h1": int(teY.iloc[j]),
                "v1_direction": int(v1_dir[j]), "v1_class": v1_cls[j],
                "v1_p_up": float(v1_p[j]),
                "logit_p_up": float(logit_p[j]), "lgbm_p_up": float(lgbm_p[j]),
            })

    pred = pd.DataFrame(rows)
    cut = pd.DataFrame(cutoffs)
    pred.to_csv(PROC / "backtest_predictions.csv", index=False)
    cut.to_csv(PROC / "walkforward_train_cutoffs.csv", index=False)

    # ---- 评估 ----
    def dir_of(p): return (p > 0.5).astype(int)

    y = pred["y_true_h1"]
    v1_acc = accuracy_score(y, pred["v1_direction"])
    logit_acc = accuracy_score(y, dir_of(pred["logit_p_up"]))
    lgbm_acc = accuracy_score(y, dir_of(pred["lgbm_p_up"]))
    logit_auc = roc_auc_score(y, pred["logit_p_up"])
    lgbm_auc = roc_auc_score(y, pred["lgbm_p_up"])

    # ---- 特征重要性（首期训练窗 LightGBM + Logistic 系数）----
    lgbm0 = m2.fit_lightgbm(ok.iloc[a0:b0][FEATURES], ok.iloc[a0:b0]["label_h1"])
    imp = m2.feature_importance_lgbm(lgbm0)
    logit0 = m2.fit_logistic(ok.iloc[a0:b0][FEATURES], ok.iloc[a0:b0]["label_h1"])
    coef = pd.Series(logit0.named_steps["logisticregression"].coef_[0], index=FEATURES)

    _plot_importance(imp, coef)
    _write_report(pred, cut, best_weights, imp, coef,
                  v1_acc, logit_acc, lgbm_acc, logit_auc, lgbm_auc)

    print("== P3 Walk-Forward 回测汇总 ==")
    print(f"folds={len(folds)}  测试样本={len(pred)}  训练窗口={TRAIN_WEEKS}周 步长={STEP_WEEKS}周")
    print(f"v1 权重(sd,hdd,supply,eu) = {np.round(best_weights, 4)}")
    print(f"方向命中率: v1={v1_acc:.4f}  logistic={logit_acc:.4f}  lightgbm={lgbm_acc:.4f}")
    print(f"AUC: logistic={logit_auc:.4f}  lightgbm={lgbm_auc:.4f}")
    print(f"首期训练 {cut.iloc[0]['train_start']}~{cut.iloc[0]['train_end']}  末期测试 {cut.iloc[-1]['test_start']}~{cut.iloc[-1]['test_end']}")


def _plot_importance(imp: pd.Series, coef: pd.Series):
    import os
    os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".mplconfig"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # 中文字体（Windows 下用微软雅黑/黑体，缺失则回退）
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    labels = {"sd_feature": "库存偏离度 SD", "hdd_anomaly_4wk": "HDD 4周异常",
              "supply_shock_4wk_z": "供给冲击 Z", "eu_storage_deviation": "欧盟储气偏离"}
    names = [labels[c] for c in imp.index]

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    axes[0].barh(names[::-1], imp.values[::-1], color="#4C72B0")
    axes[0].set_title("LightGBM 特征重要性（首期训练窗）")
    axes[0].set_xlabel("importance")
    axes[1].barh(names[::-1], coef.values[::-1], color="#DD8452")
    axes[1].set_title("Logistic 系数（首期训练窗）")
    axes[1].set_xlabel("coefficient")
    for ax in axes:
        ax.axvline(0, color="gray", lw=0.8)
    fig.tight_layout()
    fig.savefig(DOCS / "feature_importance.png", dpi=110)
    plt.close(fig)


def _write_report(pred, cut, weights, imp, coef, v1_acc, logit_acc, lgbm_acc, logit_auc, lgbm_auc):
    y = pred["y_true_h1"]
    lines = [
        "# P3 回测报告（Walk-Forward）",
        "",
        "> 个人研究项目，输出不构成交易建议。只输出方向概率/松紧等级，不输出价格点位。",
        "",
        "## 1. 设定",
        f"- 滚动训练窗 {TRAIN_WEEKS} 周（3 年），步长 {STEP_WEEKS} 周，每期外推 {TEST_WEEKS} 周；",
        f"- 首期训练 {cut.iloc[0]['train_start']}~{cut.iloc[0]['train_end']}，测试 {cut.iloc[0]['test_start']} 起；",
        f"- 末期测试 {cut.iloc[-1]['test_start']}~{cut.iloc[-1]['test_end']}；共 {len(cut)} 折、{len(pred)} 个 OOS 周样本。",
        "- as-of 铁律：月频因子 shift 3 个月（13 周）；周储气 shift 1 周（周四发布）；基线只用历史年份。",
        "- v1 权重仅在首期训练窗内 expanding 时序 CV 网格搜索，之后全程固定；v2 超参固定不调。",
        "",
        "## 2. 整体结果（方向命中率 / AUC，标签=未来1周 HH 周均值涨跌）",
        "",
        "| 模型 | 方向命中率 | AUC |",
        "| --- | --- | --- |",
        f"| v1 规则压力评分 | {v1_acc:.4f} | — |",
        f"| Logistic（主基线） | {logit_acc:.4f} | {logit_auc:.4f} |",
        f"| LightGBM（对比） | {lgbm_acc:.4f} | {lgbm_auc:.4f} |",
        "",
        "注：随机/惯性双基准、分情景命中率、ROC-AUC 与失效案例复盘详见 P4 风控评估；"
        "此处仅报告方向命中率与 AUC 的 OOS 均值，不构成交易建议。",
        "",
        "## 3. v1 权重（业务符号：SD/供给/欧盟 偏空，HDD 偏多）",
        "",
        f"`sd={weights[0]:.4f}  hdd={weights[1]:.4f}  supply={weights[2]:.4f}  eu={weights[3]:.4f}`",
        "",
        "## 4. 特征重要性（首期训练窗）",
        "",
        "![特征重要性](feature_importance.png)",
        "",
        "## 5. 每折详情（walkforward_train_cutoffs.csv）",
        "",
        "| fold | train_start | train_end(截止) | test_start | test_end | n_train | n_test |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for _, r in cut.iterrows():
        lines.append(f"| {r['fold']} | {r['train_start']} | {r['train_end']} | "
                     f"{r['test_start']} | {r['test_end']} | {r['n_train']} | {r['n_test']} |")
    lines += [
        "",
        "## 6. 红线自检",
        "",
        "- 未输出任何价格点位；只输出 P(up) 与 宽松/中性/偏紧；",
        "- Walk-Forward 滚动向前，v1 权重首期确定后全程固定，无全样本拟合、无最优区间挑选；",
        "- 月频 shift 3 个月、周储气 shift 1 周、基线只用历史（防未来信息）；",
        "- 无滞后中国数据入特征。",
    ]
    (DOCS / "backtest_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
