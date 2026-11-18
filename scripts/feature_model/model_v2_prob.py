# -*- coding: utf-8 -*-
"""
P3 · L3 v2 方向概率模型：Logistic 主基线 + LightGBM 对比。

- 仅用 L2 四因子；输出 P(up) ∈ [0,1]（方向概率，非点位）。
- 拟合用 sklearn StandardScaler（训练集统计）+ LogisticRegression / LGBMClassifier。
- 超参固定（不在测试集上调参），保证 Walk-Forward 无信息泄露。
- 红线：只输出方向概率，不输出价格点位。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

FEATURES = ["sd_feature", "hdd_anomaly_4wk", "supply_shock_4wk_z", "eu_storage_deviation"]


def _as_array(X: pd.DataFrame) -> pd.DataFrame:
    return X[FEATURES].astype(float).fillna(0.0)


def fit_logistic(X: pd.DataFrame, y: pd.Series):
    model = make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=2000, C=1.0, class_weight=None, solver="lbfgs"),
    )
    model.fit(_as_array(X), y.astype(int))
    return model


def fit_lightgbm(X: pd.DataFrame, y: pd.Series):
    import lightgbm as lgb
    model = lgb.LGBMClassifier(
        n_estimators=200, learning_rate=0.05, max_depth=3,
        num_leaves=15, subsample=0.9, colsample_bytree=0.9,
        random_state=42, verbosity=-1,
    )
    model.fit(_as_array(X), y.astype(int))
    return model


def predict_proba(model, X: pd.DataFrame) -> np.ndarray:
    return model.predict_proba(_as_array(X))[:, 1]


def feature_importance_lgbm(model, names=FEATURES) -> pd.Series:
    return pd.Series(model.feature_importances_, index=names)
