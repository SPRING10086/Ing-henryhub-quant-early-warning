# 清洗日志（P2 数据治理）

> 项目：全球天然气（Henry Hub / TTF 代理）量化研判原型
> 版本：P2 · 2026-11-18
> 生成脚本：`scripts/data_pipeline/build_weekly.py`（可复现全链路）
> 免责声明：本项目为个人研究项目，输出不构成交易建议。
>
> 本日志记录所有折算、填充、异常处理，做到**每个数字可溯源到 raw 行**。

## 0. 红线合规自检

| 红线 | 状态 |
| --- | --- |
| raw 只读、不覆盖 | ✅ 脚本仅 `read_csv`/`read_text`，写入只进 `data/processed/` 与 `docs/` |
| 无记录不删、不填 | ✅ 所有聚合/折算/标记均在本日志留痕；无隐式 dropna/ffill |
| -999 → NaN | ✅ 防御性转换（本批 raw 无 -999，未触发） |
| 滞后中国数据禁入周度特征 | ✅ 本脚本不涉及中国数据（仅背景定性，不入表） |
| 全部折算留痕 | ✅ 见 §2–§5 逐项 |
| 不编造 | ✅ 月频止于 2026-08、储气止于 2026-11-06，未前向外推 |

## 1. 输入（data/raw，只读）

| 源 | 文件 | 关键事实 |
| --- | --- | --- |
| FRED Henry Hub 现货 | fred_dhhngsp_20261118_000138.csv | 日频 1997-01-07~2026-11-11，7787 行，DHHNGSP USD/MMBtu；周末/假日缺失 2059 行（26.44%） |
| EIA 周储气 | eia_storage_wkly_20261118_005436.json | 周频 880 周 2010-01-01~2026-11-06，series NW2_EPG0_SWO_R48_BCF（Lower 48 working gas），BCF |
| EIA 月产量 | eia_drygas_prod_monthly_20261118_005437.json | 月频 200 月 2010-01~2026-08，N9070US2（U.S. Dry Production），MMCF |
| EIA 月 LNG 出口 | eia_lng_exports_monthly_20261118_005439.json | 月频 200 月 2010-01~2026-08，N9133US2（Liquefied U.S. LNG Exports），MMCF |
| NOAA HDD（历史） | noaa_hdd_states_hist_20261118_001237.csv | 日频 2010-01-01~2025-12-31，48 州人口加权，280512 行 |
| NOAA HDD（当年） | noaa_hdd_states_20261118_000145.csv | 日频 2026-01-01~2026-11-15，15312 行 |
| GIE-AGSI EU | gie_agsi_eu_20261118_013648.json | 日频 2011-01-01~2026-11-15，5798 gas day，欧洲 20 国体积加权聚合 |

## 2. FRED 日→周（周平均）

- 规则：每个交易日归入其所属"结束于周五"周；`henry_hub_avg = mean(DHHNGSP 有效值)`；
  `henry_hub_obs_count = count(有效值)`；缺失周保留 NaN（本批 2010+ 每周均有交易日，无缺失周）。
- 缺失处理：`DHHNGSP` 中 `.`/空/非数值 → `pd.to_numeric(errors='coerce')` → NaN（不填充）。
- 溯源示例：`weekly_master.henry_hub_avg[2010-01-08]` = 该周 2010-01-04~01-08 五个交易日 DHHNGSP 的均值。
- 输出：henry_hub_weekly.csv（1997+ 全历史 1558 周）；主表仅取 2010-01-01 起（其他源对齐窗口）。

## 3. EIA 周储气（原生周频，只对齐不折算）

- 规则：`period` 已是周五，`storage_bcf = value`（BCF），直接并入主表，**无任何插值/填充**。
- 时间错位标注：数据截至周五，**报告周四发布**（release_lag≈+6 天）。主表保留数值原值，
  但 P3 进特征须按 `as-of = period + 6天` shift，防未来信息。
- 缺失：最后一周 2026-11-13 无值（源止于 2026-11-06），保留 NaN，不前向外推。
- 溯源示例：`weekly_master.storage_bcf[2010-01-01] = 3117` ← raw `eia_storage_wkly_*.json` period=2010-01-01 行 value=3117。

## 4. EIA 月产量 / 月 LNG 出口（月频平铺折算到周·近似值）

- 规则：`month_key = anchor 所在月份(YYYY-MM)`；`production_mmcf[anchor] = 该月 value`；
  标记 `production_is_monthly_flattened=1`（非空行）；`production_month` 记录溯源月份。
  LNG 出口同理。
- **折算性质**：这是**月频平铺到周的近似值**（当月值赋给该月内所有周五），非原生周频观测，
  已在数据字典与主表标记列显式标注。
- **发布滞后**：EIA 月频发布滞后约 1–2 月；源止于 2026-08，故 2026-09-04 起 11 周为 NaN
  （**未前向外推**，P3 进特征须 shift 至 as-of 可用之后）。
- 溯源示例：`weekly_master.production_mmcf[2010-01-01..2010-01-29] = 1737233`
  ← raw `eia_drygas_prod_monthly_*.json` period=2010-01 行 value=1737233；
  `production_month=2010-01`，`production_is_monthly_flattened=1`。

## 5. NOAA HDD（人口加权 CONUS 聚合 + 周累加）

- 规则：
  1) hist + latest 两份拼接（无重叠，2010-01-01~2026-11-15 连续）；
  2) `hdd` 转数值，`-999 → NaN`（防御，本批未触发）；
  3) 仅取 48 个 CONUS 州；按日 pivot 成宽表；
  4) `CONUS_daily = Σ(state_hdd × POP_2010[state]) / Σ(POP_2010[有值州])`（人口加权）；
  5) `hdd_conus_weekly_sum = Σ(CONUS_daily)`（周内 Sat–Fri 七日）；`hdd_obs_count` 记有效日数。
- 权重来源：US Census Bureau 2010 Census 各州常住人口（固定权重，无未来信息）。
- 异常/缺失：日频缺失即该日 NaN，聚合时按有值州归一；不删州、不填日。
- 溯源示例：`weekly_master.hdd_conus_weekly_sum[2010-01-08]` = 2010-01-02~01-08 七日
  CONUS 人口加权日 HDD 之和（首周 2010-01-01 仅 1 日，obs_count=1）。
- 输出：noaa_hdd_conus_daily.csv（6163 日）+ noaa_hdd_weekly.csv。

## 6. GIE-AGSI EU（从 per-country 原始值重建正确欧盟聚合 + 周快照）

- 规则：从 raw `rows_by_country` 逐国原始值重建（**不改写 raw**）：
  1) `capacity_twh = Σ workingGasVolume`（工作气容量，TWh）；`inventory_twh = Σ gasInStorage`（库存，TWh）；
  2) `full_pct = inventory / capacity × 100`（**容量加权正确公式**）；
  3) `countries_reported = 有 workingGasVolume 的国家数`；
  4) 周频取周内最后一日（周五）快照（`last`，非均值）。
- **单位核实结论**：`workingGasVolume`/`gasInStorage` 单位均为 **TWh**（详见数据字典 §4）。
- **修正 P1 聚合缺陷**：P1 用 `eu_full = Σwgv / Σ(wgv/(full/100))`，量纲错乱，当比利时
  2018-04-13 报 `full=0.01%`（库存≈0）时，分母被 `wgv/full≈9万 TWh` 主导，把欧盟充满度
  错误压到 1.07%（实际 19.50%）。P2 改为 `ΣgasInStorage/ΣworkingGasVolume×100`，修正后
  欧盟充满度范围 17.99%~99.38%（中位 66.39%）。此为有记录的折算口径修正，未改写 raw。
- 口径（继承 P1 探测结论）：country=eu 直取不可用，改用欧洲 20 国聚合；SE 无地下储气 0 行，
  其余 19 国有数据；IE/GB 个别日无值按 NaN 处理（skipna 聚合）。
- 缺失：2010 全年 53 周无 GIE（AGSI 最早 2011-01-01），保留 NaN。
- 溯源示例：`weekly_master.gie_full_pct[2018-04-13] = 19.497`
  = ΣgasInStorage(208.438) / ΣworkingGasVolume(1069.096) × 100
  ← raw `gie_agsi_eu_*.json` `rows_by_country` 各该国 gasDayStart=2018-04-13 行。

## 7. 主表合并与缺失总览

- 周轴：连续周五 2010-01-01 ~ 2026-11-13（881 周），各源 left-join。
- 缺失（周）：henry_hub_avg 0；storage_bcf 1；production/lng 11；hdd 0；gie 53。
- 末周边界说明：HDD/GIE 日频的 2026-11-14/15 两日归入 2026-11-20 周，超出主表轴，
  未计入主表周值，但仍保留在 noaa_hdd_conus_daily.csv 与 raw 中。

## 8. 可复现

```bash
# 在项目根目录（.venv 已含 pandas/numpy/requests/python-dotenv）
.venv\Scripts\python.exe scripts\data_pipeline\build_weekly.py
```
运行后重写 `data/processed/` 下 8 个 P2 产物（幂等），并打印缺失汇总。
