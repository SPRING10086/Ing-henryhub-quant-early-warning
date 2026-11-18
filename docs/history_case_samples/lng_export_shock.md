# 历史情景样本 3：LNG 出口扰动期（Freeport 停产）

> 个人研究项目，输出不构成交易建议。严格 as-of，禁事后数据。数据可溯源 `data/processed/`（→ `data/raw/`）。

## 1. 情景概述

2022-06-08 美国 Freeport LNG 终端（Texas，设计产能约 20 亿立方英尺/日）发生火灾，停产约 8 个月，
美国 LNG 出口能力骤降。该事件与 2022 欧洲能源危机（俄乌冲突）时间重叠，同期 Henry Hub 冲高至
2022-08-26 见顶 9.56 USD/MMBtu 后回落。

## 2. 时间范围与 as-of 口径

- 复盘区间：fold 2（2022-07-15 ~ 2022-10-07），Freeport 停产期内。
- 训练截止日（as-of）：**2022-07-08**（fold 2 `train_end`，见 `walkforward_train_cutoffs.csv`）。
- **关键滞后事实**：LNG 出口为**月频且滞后约 2.5 月**（EIA v2 无周频路由），P3 特征又 shift 3 个月；
  故 2022-07/08 时点，模型可用的 LNG 特征实际反映的是 **2022-04（火灾前）** 的出口水平——
  即「Freeport 冲击」在事件发生后数月内**尚未进入模型特征**（见 `docs/risk_label_rules.md` R2 注意）。

## 3. 当时可获得数据（取值周 = train_end 2022-07-08）

| 指标 | 当时值 | 来源字段（data/processed/） |
| --- | --- | --- |
| 周储气（shift 1 周，最后可用） | 2311 Bcf | features_weekly.csv.storage_shift1 |
| 库存偏离度 SD（as-of） | -0.92（低于 5 年同期） | features_weekly.csv.sd_feature |
| 月产量（shift 3 月，2022-04） | 2,959,139 MMcf | features_weekly.csv.production_shift13 |
| HDD 4 周异常 | +1.5 | features_weekly.csv.hdd_anomaly_4wk |
| 供给冲击 4 周 Z（产量） | +0.90 | features_weekly.csv.supply_shock_4wk_z |
| 欧盟储气充满度 | 61.22% | features_weekly.csv.gie_full_pct |
| 欧盟储气偏离 | -0.10 pct | features_weekly.csv.eu_storage_deviation |
| LNG 出口 52 周滚动 Z（trailing，train_end 行） | -0.79（负但未达 -1 黄线） | risk_eval_weekly.csv.lng_z52 |
| HH 周收益 8 周波动（trailing，train_end 行） | 0.0977（略超 P75，R4 黄） | risk_eval_weekly.csv.hh_vol8 |

> 注：`lng_z52`/`hh_vol8` 为 trailing 指标，train_end 当周（2022-07-08）在 `risk_eval_weekly.csv` 有行，
> 属当时可得。`is_shock_freeport` 为 P4 事后标注，非当时信号。

## 4. 模型当时读数（对 2022-07-15 当周方向，取 backtest_predictions.csv 的 test_start 预测）

| 模型 | P(up) | 方向 | 来源字段 |
| --- | --- | --- | --- |
| v1 规则压力评分 | 0.470 | 看空（松紧等级：中性） | backtest_predictions.csv.v1_p_up |
| Logistic | 0.438 | 看空 | backtest_predictions.csv.logit_p_up |
| LightGBM | 0.551 | 看多 | backtest_predictions.csv.lgbm_p_up |

## 5. 实际走势与复盘

- fold 2 实际：HH 冲高至 2022-08-26（9.56 USD/MMBtu）见顶后快速回落；v1 命中 0.385、Logistic 0.462、LightGBM 0.538。
- Freeport 扰动期（is_shock_freeport=1 各周）LightGBM 命中率 0.684，为各场景中最高，但整体仍属「事件后随价格波动」，
  未体现对「出口骤降→供需失衡」的前瞻定价。

## 6. 结论（引用 docs/failure_case_library.md + docs/risk_label_rules.md）

- 根因：LNG 出口因**月频滞后**在事发时不可见（train_end 2022-07-08 的 lng_z52 仅 -0.79，尚未破 -1），
  模型无法实时感知 Freeport 冲击；且该事件与地缘风险溢价（案例 1）叠加，方向信号被风险溢价主导。
- 复盘边界：R2（LNG 出口扰动）是**回溯性/滞后**监控，不具实时性；`is_shock_freeport` 为 P4 事后标注，非当时信号。
- 改进方向：对「出口骤降 + 波动率放大」组合设事后归因标签（不用于实时决策）；
  出口类冲击的前瞻定价需更高频的船期/运费代理数据（本红线禁止付费数据，故暂缺）。
