# 历史情景样本 1：2022 欧洲能源危机

> 个人研究项目，输出不构成交易建议。本样本为 P5 报告工程的历史情景模拟，**严格使用当时（as-of）可获得数据**，
> 禁止事后信息。数据可溯源 `data/processed/`（→ `data/raw/`）。

## 1. 情景概述

2022-02-24 俄乌冲突爆发，欧洲天然气恐慌性采购 + 全球 LNG 抢购，Henry Hub 出现历史级风险溢价，
周均价从 ~3.8 USD/MMBtu 快速上行至 2022-08-26 见顶 9.56 USD/MMBtu。

## 2. 时间范围与 as-of 口径

- 复盘区间：fold 0（2022-01-14 ~ 2022-04-08，危机爆发期）；延伸 fold 2（2022-07-15 ~ 2022-10-07，见顶与崩塌）。
- 训练截止日（as-of）：**2022-01-07**（fold 0 的 `train_end`，见 `walkforward_train_cutoffs.csv`）。
- shift 口径（沿用 P2/P3）：周储气 shift 1 周（周四发布）；月频产量/LNG 出口 shift 3 个月（13 周）；
  欧盟充满度为当周可得值（AGSI 日频滞后 ~1–2 日）。

## 3. 当时可获得数据（取值周 = train_end 2022-01-07）

| 指标 | 当时值 | 来源字段（data/processed/） |
| --- | --- | --- |
| 周储气（shift 1 周，最后可用） | 3195 Bcf | features_weekly.csv.storage_shift1 |
| 库存偏离度 SD（as-of） | +0.14（略高于 5 年同期） | features_weekly.csv.sd_feature |
| 月产量（shift 3 月，2021-10） | 3,013,375 MMcf | features_weekly.csv.production_shift13 |
| HDD 4 周异常 | -79.1（偏暖） | features_weekly.csv.hdd_anomaly_4wk |
| 供给冲击 4 周 Z | +1.02 | features_weekly.csv.supply_shock_4wk_z |
| 欧盟储气充满度 | 52.10% | features_weekly.csv.gie_full_pct |
| 欧盟储气偏离 | -17.76 pct（明显偏低） | features_weekly.csv.eu_storage_deviation |

> 风险标签（R1–R4）为 P4 事后评估、非当时信号，不列入上表（见第 5 节复盘）。

## 4. 模型当时读数（对 2022-01-14 当周方向，取 backtest_predictions.csv 的 test_start 预测）

| 模型 | P(up) | 方向 | 来源字段 |
| --- | --- | --- | --- |
| v1 规则压力评分 | 0.471 | 看空（松紧等级：中性） | backtest_predictions.csv.v1_p_up |
| Logistic | 0.719 | 看多 | backtest_predictions.csv.logit_p_up |
| LightGBM | 0.726 | 看多 | backtest_predictions.csv.lgbm_p_up |

## 5. 实际走势与复盘（事后，仅用于复盘，不用于当时决策）

- fold 0 实际：13 周中 **10 周上涨**（风险溢价推动），v1 在其中 11 周预测「下跌」，命中率 **3/13 = 0.231**；
  Logistic / LightGBM 同期命中 **0.692 / 0.692**（明显更优）。
- fold 2 实际：HH 冲高至 2022-08-26（9.56 USD/MMBtu）见顶后快速回落，v1 命中 0.385、Logistic 0.462、LightGBM 0.538。
- 风险标签（事后）：OOS 首周（2022-01-14）R3 欧盟储气低位·黄、R4 高波动·黄触发；这些为 P4 回溯评估，非当时信号。

## 6. 结论（引用 docs/failure_case_library.md 案例 1）

- 根因是**结构性 regime shift**：训练窗（2019–2022-01）为低波动过剩 regime，测试期突变为地缘政治风险溢价 regime；
  v1 权重（supply=0.8 / eu=0.2，偏空符号）与校准全部失效。
- 复盘边界：v1 为「纯基本面 + 固定符号」规则，**无风险溢价/波动率维度**，对非基本面冲击天然盲区。
- 改进方向（引用失效案例库）：引入波动率/风险溢价特征作为「冲击开关」，冲击期强制中性或降低置信度。
