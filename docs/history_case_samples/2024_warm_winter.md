# 历史情景样本 2：2024 暖冬（El Niño）

> 个人研究项目，输出不构成交易建议。严格 as-of，禁事后数据。数据可溯源 `data/processed/`（→ `data/raw/`）。

## 1. 情景概述

2023–24 年 El Niño 暖冬，取暖需求持续偏弱，叠加高库存、高产量，Henry Hub 周均价持续单边下行，
2024-03-15 触及区间低点（约 1.4 USD/MMBtu），属「暖冬 + 高库存 + 高产量」的持续性阴跌。

## 2. 时间范围与 as-of 口径

- 复盘区间：fold 8（2024-01-12 ~ 2024-04-05）。
- 训练截止日（as-of）：**2024-01-05**（fold 8 `train_end`，见 `walkforward_train_cutoffs.csv`）。
- shift 口径（沿用 P2/P3）：周储气 shift 1 周；月频产量/LNG 出口 shift 3 个月。

## 3. 当时可获得数据（取值周 = train_end 2024-01-05）

| 指标 | 当时值 | 来源字段（data/processed/） |
| --- | --- | --- |
| 周储气（shift 1 周，最后可用） | 3476 Bcf | features_weekly.csv.storage_shift1 |
| 库存偏离度 SD（as-of） | +1.47（显著高于 5 年同期） | features_weekly.csv.sd_feature |
| 月产量（shift 3 月，2023-10） | 3,212,305 MMcf | features_weekly.csv.production_shift13 |
| HDD 4 周异常 | -72.6（明显偏暖） | features_weekly.csv.hdd_anomaly_4wk |
| 供给冲击 4 周 Z | +0.30 | features_weekly.csv.supply_shock_4wk_z |
| 欧盟储气充满度 | 84.96%（高于季节性） | features_weekly.csv.gie_full_pct |
| 欧盟储气偏离 | +15.92 pct | features_weekly.csv.eu_storage_deviation |

> 风险标签（R1–R4）为 P4 事后评估、非当时信号。train_end 当周（2024-01-05）`risk_eval_weekly.csv`
> 显示 hh_vol8=0.064（未超 P75 0.089，R4 未触发）、hdd_anom_z=-1.25（偏暖侧，R1 无暖侧阈值）。

## 4. 模型当时读数（对 2024-01-12 当周方向，取 backtest_predictions.csv 的 test_start 预测）

| 模型 | P(up) | 方向 | 来源字段 |
| --- | --- | --- | --- |
| v1 规则压力评分 | 0.431 | 看空（松紧等级：中性） | backtest_predictions.csv.v1_p_up |
| Logistic | 0.712 | 看多（逆势抄底） | backtest_predictions.csv.logit_p_up |
| LightGBM | 0.463 | 看空 | backtest_predictions.csv.lgbm_p_up |

## 5. 实际走势与复盘

- fold 8 实际：13 周中 **10 周下跌**，持续单边下行。
- **Logistic 在其中 10 周预测「上涨」**（反复押注超跌反弹），命中率仅 **5/13 = 0.385**；
  而 v1（全程看跌）命中 0.769、LightGBM 0.846。三模型方向分歧极大（v1/lgbm 顺趋势看空、Logistic 逆势抄底）。

## 6. 结论（引用 docs/failure_case_library.md 案例 2）

- 根因：Logistic 线性 + 特征均值回归倾向，在「低位 + 低波动 + 供给过剩」的持续性阴跌中，
  于「继续跌 vs 反弹」间反复误判，缺乏趋势跟踪能力。
- 复盘边界：三模型在暖冬/阴跌 regime 分歧极大，单模型结论不可靠。
- 改进方向（引用失效案例库）：引入趋势/动量特征或直接用 LightGBM 捕获持续性趋势；
  对「低位 + 低波动 + 过剩」组合设 regime 标签，抑制逆势抄底；报告三模型一致性作为置信度，分歧大时降级为中性。
