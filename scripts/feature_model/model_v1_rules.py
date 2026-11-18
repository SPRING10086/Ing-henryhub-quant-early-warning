# -*- coding: utf-8 -*-
"""
P3 · L3 v1 规则压力评分模型（可解释、四因子加权、三档输出）。

- 业务符号（先验）：库存偏离度 SD / 供给冲击 / 欧盟储气偏离 偏空（负贡献），
  HDD 4 周异常偏多（正贡献）。
- 拟合：对训练集做 z-score 标准化；网格搜索权重 w_i ∈ {0,.25,.5,.75,1}（归一化），
  目标 = 在首个训练窗内做 expanding-window 时序 CV 的方向命中率最大化；
  权重确定后全程固定（防全样本拟合）。
- 输出：pressure_score（连续，正=偏紧/偏多）、direction（score>0 → 1/up）、
  classify（宽松/中性/偏紧，阈值 0.5 个标准化单位）、p_up（sigmoid 校准的伪概率，仅参考）。
- 红线：只输出方向/松紧，不输出价格点位。
"""
from __future__ import annotations

from itertools import product

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score

FEATURES = ["sd_feature", "hdd_anomaly_4wk", "supply_shock_4wk_z", "eu_storage_deviation"]
SIGNS = np.array([-1.0, 1.0, -1.0, -1.0])  # sd/supply/eu 偏空(-)，hdd 偏多(+)
WEIGHT_GRID = [0.0, 0.25, 0.5, 0.75, 1.0]
CLASS_THRESHOLD = 0.5  # 标准化分数阈值（三档：<=-0.5 宽松 / 中间 中性 / >=0.5 偏紧）


class V1Rules:
    def __init__(self):
        self.weights = None
        self.mu = None
        self.sigma = None

    def fit_scaler(self, X: pd.DataFrame):
        """按当前训练集拟合 z-score 均值/标准差（Walk-Forward 每折独立拟合，防泄露）。"""
        X = X.reset_index(drop=True).astype(float)
        self.mu = X[FEATURES].mean()
        self.sigma = X[FEATURES].std(ddof=0).clip(lower=1e-9)
        return self

    def _zscore(self, X: pd.DataFrame) -> np.ndarray:
        X = X[FEATURES].astype(float)
        return (X - self.mu) / self.sigma.replace(0, np.nan)

    def score(self, X: pd.DataFrame) -> np.ndarray:
        Xz = self._zscore(X)
        # 缺失因子按 0 处理（无信息）
        Xz = np.nan_to_num(Xz, nan=0.0)
        return Xz @ (self.weights * SIGNS)

    def direction(self, X: pd.DataFrame) -> np.ndarray:
        return (self.score(X) > 0).astype(int)

    def classify(self, X: pd.DataFrame) -> np.ndarray:
        s = self.score(X)
        out = np.where(s <= -CLASS_THRESHOLD, "宽松",
                       np.where(s >= CLASS_THRESHOLD, "偏紧", "中性"))
        return out

    def p_up(self, X: pd.DataFrame) -> np.ndarray:
        """sigmoid(score) 作为参考概率（非校准，仅供排序）。"""
        from scipy.special import expit
        return expit(self.score(X))

    def fit(self, X: pd.DataFrame, y: pd.Series, internal_val_weeks: int = 26):
        """网格搜索权重：在 X 训练集内部做 expanding-window 时序 CV。"""
        self.fit_scaler(X)
        X = X.reset_index(drop=True).astype(float)
        y = np.asarray(y, dtype=float)
        Xz = self._zscore(X)
        Xz = np.nan_to_num(Xz, nan=0.0)

        n = len(X)
        best = None
        best_acc = -1.0
        for w in product(WEIGHT_GRID, repeat=4):
            if sum(w) == 0:
                continue
            wn = np.array(w) / sum(w) * SIGNS  # 归一化权重 × 业务符号
            s = Xz @ wn
            pred = (s > 0).astype(int)
            # expanding-window 内部时序 CV
            accs = []
            for val_start in range(n // 2, n, internal_val_weeks):
                if val_start + internal_val_weeks > n:
                    val_start = max(n // 2, n - internal_val_weeks)
                acc = accuracy_score(y[val_start:val_start + internal_val_weeks],
                                     pred[val_start:val_start + internal_val_weeks])
                accs.append(acc)
                if val_start + internal_val_weeks >= n:
                    break
            acc = float(np.mean(accs)) if accs else float(accuracy_score(y, pred))
            if acc > best_acc:
                best_acc = acc
                best = wn
        self.weights = best / SIGNS  # 存回不带符号的归一化权重
        return self


def main():
    """独立演示：读取特征表，用首个训练窗拟合 v1，打印权重。"""
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[2]
    f = pd.read_csv(ROOT / "data" / "processed" / "features_weekly.csv", parse_dates=["week_anchor"])
    f = f.sort_values("week_anchor").reset_index(drop=True)
    ok = f.dropna(subset=FEATURES + ["label_h1"]).reset_index(drop=True)
    # 首个训练窗：2019-01-04 起 156 周
    start = ok[ok.week_anchor >= "2019-01-04"].index[0]
    tr = ok.iloc[start:start + 156]
    m = V1Rules().fit(tr[FEATURES], tr["label_h1"])
    print("v1 权重（sd, hdd, supply, eu）:", np.round(m.weights, 4))
    print("首个训练窗方向命中率:", round(accuracy_score(tr["label_h1"], m.direction(tr[FEATURES])), 4))
    print("三档分布:", pd.Series(m.classify(tr[FEATURES])).value_counts().to_dict())


if __name__ == "__main__":
    main()
