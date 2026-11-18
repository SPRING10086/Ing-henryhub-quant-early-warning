# 数据字典（P2 数据治理）

> 项目：全球天然气（Henry Hub / TTF 代理）量化研判原型
> 版本：P2 · 2026-11-18
> 免责声明：本项目为个人研究项目，输出不构成交易建议。
>
> 本字典描述 **data/processed/ 周频对齐主表** 及其构件。原始文件字段见
> `data/raw/` 与 `data/raw/missing_report_*.md`（P1 采集缺失率统计）。

## 1. 统一口径（先读）

| 口径 | 规定 |
| --- | --- |
| 周锚点 | 每周五（"week ending Friday"，与 EIA 周储气报告周期一致）。任意日期归入其所属周的周五。 |
| 价格周频 | FRED 日频 DHHNGSP 按周对**有效观测取算术平均**（周末/假日缺失不参与，缺失周不填充）。 |
| 库存周频 | EIA 周储气 period 本身即周五，直接对齐；**报告周四发布、数据截至上周五**（见 as-of 说明）。 |
| 月频→周频 | EIA 月产量/月 LNG 出口**平铺折算**：当月值赋给该月内所有周五锚点，`is_monthly_flattened=1` 标记；属**近似值**。 |
| HDD 周频 | 人口加权 CONUS 日频 **周累加**（度日加总）。 |
| GIE 周频 | 日频取周内最后一日（周五）快照值（`last`）。 |
| 缺失处理 | `-999→NaN`；不删样本；不无记录填充。 |

### 1.1 时间错位（as-of / release lag，P3 回测防未来信息的关键）

| 指标 | 数据时点 | 可获取时点 | 进周度特征要求 |
| --- | --- | --- | --- |
| 周储气 storage_bcf | 截至周五 | **次周四发布**（≈ period + 6 天） | 必须 shift 至 as-of 可用之后 |
| 月产量 production_mmcf | 当月 | **滞后约 1–2 月**（EIA 月报/STEO） | 必须 shift 至 as-of 可用之后 |
| 月 LNG 出口 lng_export_mmcf | 当月 | **滞后约 1–2 月** | 必须 shift 至 as-of 可用之后 |
| HDD / 价格 / GIE | 同期（日频） | 基本同期（次日更新） | 可同期使用 |

## 2. 主表：`data/processed/weekly_master.csv`

- 行数：881 周（2010-01-01 ~ 2026-11-13，周五）
- 键：`week_anchor`

| 字段 | 单位 | 频率 | 来源（raw） | 缺失(周) | 是否折算 | 是否允许进周度特征 |
| --- | --- | --- | --- | --- | --- | --- |
| week_anchor | 日期 | 周 | 派生（周五轴） | 0 | — | 键，不参与建模 |
| henry_hub_avg | USD/MMBtu | 周 | fred_dhhngsp_*.csv | 0/881 | 日→周均值（聚合） | 是（目标/价格特征） |
| henry_hub_obs_count | 次 | 周 | 同上 | 0 | 派生（有效日计数） | 辅助，不入特征 |
| storage_bcf | BCF | 周 | eia_storage_wkly_*.json | 1/881 | 否（原生周频） | 是，须 shift 至周四发布后 |
| production_mmcf | MMCF | 月→周 | eia_drygas_prod_monthly_*.json | 11/881 | **是**（月频平铺折算，近似值） | 是，须 shift 至滞后 1–2 月后 |
| production_month | YYYY-MM | 月 | 同上 | 11 | 派生（溯源用） | 辅助，不入特征 |
| production_is_monthly_flattened | 0/1 | 月→周 | 同上 | — | 标记（1=折算） | 辅助，不入特征 |
| lng_export_mmcf | MMCF | 月→周 | eia_lng_exports_monthly_*.json | 11/881 | **是**（月频平铺折算，近似值） | 是，须 shift 至滞后 1–2 月后 |
| lng_export_month | YYYY-MM | 月 | 同上 | 11 | 派生（溯源用） | 辅助，不入特征 |
| lng_export_is_monthly_flattened | 0/1 | 月→周 | 同上 | — | 标记（1=折算） | 辅助，不入特征 |
| hdd_conus_weekly_sum | 度日(HDD) | 周 | noaa_hdd_states_*.csv（48 州人口加权） | 0/881 | 是（人口加权聚合 + 周累加） | 是（同期天气，无滞后） |
| hdd_obs_count | 日 | 周 | 同上 | 0 | 派生（有效日计数） | 辅助，不入特征 |
| gie_full_pct | % | 周 | gie_agsi_eu_*.json（per-country 重建） | 53/881 | 是（容量加权 ΣgasInStorage/ΣworkingGasVolume，**修复 P1 公式缺陷**） | 是（TTF 代理，基本同期） |
| gie_wgv_twh | TWh | 周 | 同上 | 53/881 | 是（Σ workingGasVolume = 工作气**总容量**） | 是（TTF 代理） |
| gie_in_storage_twh | TWh | 周 | 同上 | 53/881 | 是（Σ gasInStorage = 当前**库存**） | 是（TTF 代理） |
| gie_countries_reported | 国 | 周 | 同上 | 53 | 派生（覆盖国数） | 辅助，不入特征 |

> 缺失说明：
> - `storage_bcf` 缺 1 周：最后一周 2026-11-13（源止于 2026-11-06，未前向外推）。
> - `production_mmcf` / `lng_export_mmcf` 缺 11 周：2026-09-04 起（源止于 2026-08，发布滞后，**未前向外推**）。
> - `gie_*` 缺 53 周：2010 全年（AGSI 最早 2011-01-01）。

## 3. 中间构件（data/processed/）

| 文件 | 内容 | 行数/范围 |
| --- | --- | --- |
| henry_hub_weekly.csv | FRED 周平均（1997+ 全历史） | 1558 周 |
| eia_storage_weekly.csv | 周储气（原生周频） | 880 周 |
| eia_production_weekly.csv | 月产量平铺折算到周 | 881 周 |
| eia_lng_export_weekly.csv | 月 LNG 出口平铺折算到周 | 881 周 |
| noaa_hdd_conus_daily.csv | CONUS 人口加权日频 HDD | 6163 日（2010-01-01~2026-11-15） |
| noaa_hdd_weekly.csv | CONUS 周累加 HDD | 881 周 |
| gie_agsi_eu_daily_corrected.csv | EU 储气日频（容量/库存/充满度，修正聚合） | 5798 日 |
| gie_agsi_eu_weekly.csv | EU 储气周快照（full/wgv/in_storage/countries） | 828 周 |

> 注：P1 遗留的 `storage_weekly.csv`、`production_weekly.csv`、`lng_export_weekly.csv`、
> `merged_weekly.csv`、`gie_agsi_eu_daily.csv` 仍保留；P2 权威输出为 `weekly_master.csv`
> 及本表所列构件（`merged_weekly.csv` 已被 `weekly_master.csv` 取代）。

## 4. 关键结论：GIE AGSI `workingGasVolume` 单位 = **TWh**

P1 曾以 `gwh` 标注（1000× 误标），后改为 `twh`。P2 核实结论：**单位为 TWh（太瓦时）**，依据：

1. **内部自洽**：`gasInStorage = workingGasVolume × full/100`。
   例（2026-11-15，奥地利）：`gasInStorage 63.1945 = workingGasVolume 100.2789 × 0.6302(full 63.02%)`，两者同单位。
2. **与各国已知工作气容量吻合（TWh 量级）**：
   DE≈247.4、IT≈203.4、NL≈144.0、FR≈123.9、AT≈100.3 TWh，与德国(~24 bcm≈250 TWh)、
   意大利(~18 bcm≈190 TWh)、荷兰(~14 bcm≈144 TWh) 等工作气容量一致；
   若为 GWh 则小 1000×（与 0.1 TWh 量级矛盾），若为 MWh 则小 1e6×。
3. **官方口径旁证**：GIE/AGSI 数据概览页以 TWh 展示储气量（如乌克兰 115.97 TWh）；
   GIE API 文档（[GIE_API_documentation](https://www.gie.eu/transparency-platform/GIE_API_documentation_v013.pdf)）
   为官方字段定义出处。

故主表/构件采用 `gie_wgv_twh`（TWh），不改写 raw 数值，仅修正单位标注。

### 4.1 修正 P1 聚合公式缺陷（重要发现）

P1 的欧盟充满度聚合公式为 `eu_full = ΣworkingGasVolume / Σ(workingGasVolume/(full/100))`，
该公式**在数学上不正确**（分母 `wgv/(full/100)` = 容量²/库存，量纲错乱）。当某国
`full` 接近 0 时（如比利时 2018-04-13 报 full=0.01%、库存≈0），分母被 `wgv/full≈9万 TWh`
主导，导致 `eu_full` 被错误压到 ~1%（实际约 19.5%）。

P2 改为**容量加权正确公式**：`eu_full = ΣgasInStorage / ΣworkingGasVolume × 100`，
并从 raw 的 `rows_by_country` 逐国原始值重建（不改写 raw）。修正后 EU 充满度范围
17.99% ~ 99.38%（中位 66.39%），2018-04-13 = 19.50%（原 1.07% 为错误值）。

字段语义澄清：`workingGasVolume` = 工作气**容量**（随设施/国家数变化，411.8→1149.9 TWh），
`gasInStorage` = 当前**库存**（季节性，158.6→1134.1 TWh）。主表同时给出两者。

## 5. HDD 人口加权口径

- 源为 NOAA CPC "StatesCONUS" **人口加权** HDD（48 个 CONUS 州，各州值已按州内人口加权）。
- CONUS 全国聚合 = 各州日频 HDD 按 **2010 美国人口普查各州常住人口** 加权平均
  （权重固定，来源 US Census Bureau 2010 Census；无未来信息，仅作聚合权重）。
- 公式：`CONUS_daily = Σ(state_hdd × pop_state) / Σ(pop_state)`（仅对有值州归一）。
- 周累加：`hdd_conus_weekly_sum = Σ(CONUS_daily)`（该周 Sat–Fri 七日）。
- `-999→NaN` 为防御性处理（本批 raw 文本经核验无 -999/-9999，未实际触发）。
