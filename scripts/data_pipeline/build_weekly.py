# -*- coding: utf-8 -*-
"""
P2 数据治理 —— raw -> 周频对齐主表（可复现全链路）。

红线遵循：
- data/raw 全程只读，绝不覆盖；
- 不无记录地删除或填充数据；所有折算/填充/异常处理在此留痕，并写入 docs/cleaning_log.md；
- 滞后中国数据不进入周度特征（本脚本不涉及中国数据，仅背景定性不入表）。

统一周频口径：
- 周锚点 = 每周五（"week ending Friday"，与 EIA 周储气报告周期一致）。
  任意日期按 week_anchor() 归入其所属周的周五；FRED 周平均按该周内实际交易日
  计算（周末/假日缺失值不参与平均，缺失周保留 NaN 不填充）。
- 时间错位（防未来信息，供 P3）：
  * EIA 周储气：period=周五数据，报告周四发布（release_lag≈+6 天）；
  * EIA 月产量 / 月 LNG 出口：月频平铺折算到周（is_monthly_flattened=1），
    发布滞后约 1–2 月，进周度特征时须 shift 至 as-of 可用之后。

输出（data/processed/，本阶段产物）：
- weekly_master.csv             # 周频对齐主表（2010-01-01 起）
- henry_hub_weekly.csv          # FRED 周平均（1997+ 全历史）
- eia_storage_weekly.csv
- eia_production_weekly.csv
- eia_lng_export_weekly.csv
- noaa_hdd_conus_daily.csv      # CONUS 人口加权日频
- noaa_hdd_weekly.csv
- gie_agsi_eu_weekly.csv
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
PROC = ROOT / "data" / "processed"

# 固定的原始文件（P1 落盘，只读）
FRED_CSV = RAW / "fred_dhhngsp_20261118_000138.csv"
EIA_STORAGE_JSON = RAW / "eia_storage_wkly_20261118_005436.json"
EIA_PROD_JSON = RAW / "eia_drygas_prod_monthly_20261118_005437.json"
EIA_LNG_JSON = RAW / "eia_lng_exports_monthly_20261118_005439.json"
NOAA_HIST_CSV = RAW / "noaa_hdd_states_hist_20261118_001237.csv"
NOAA_LATEST_CSV = RAW / "noaa_hdd_states_20261118_000145.csv"
GIE_JSON = RAW / "gie_agsi_eu_20261118_013648.json"

# ---------------------------------------------------------------------------
# 2010 US Census 各州常住人口（48 个 CONUS 州），用作 CONUS 人口加权 HDD 的固定权重。
# 来源：US Census Bureau, 2010 Census（state.1keydata.com / census.gov 2010 人口）。
# 权重固定、无未来信息（仅作人口加权聚合，不随时间变化）。
# ---------------------------------------------------------------------------
POP_2010 = {
    "AL": 4779736, "AR": 2915918, "AZ": 6392017, "CA": 37253956,
    "CO": 5029196, "CT": 3574097, "DE": 897934, "FL": 18801310,
    "GA": 9687653, "IA": 3046355, "ID": 1567582, "IL": 12830632,
    "IN": 6483802, "KS": 2853118, "KY": 4339367, "LA": 4533372,
    "MA": 6547629, "MD": 5773552, "ME": 1328361, "MI": 9883640,
    "MN": 5303925, "MO": 5988927, "MS": 2967297, "MT": 989415,
    "NC": 9535483, "ND": 672591, "NE": 1826341, "NH": 1316470,
    "NJ": 8791894, "NM": 2059179, "NV": 2700551, "NY": 19378102,
    "OH": 11536504, "OK": 3751351, "OR": 3831074, "PA": 12702379,
    "RI": 1052567, "SC": 4625364, "SD": 814180, "TN": 6346105,
    "TX": 25145561, "UT": 2763885, "VA": 8001024, "VT": 625741,
    "WA": 6724540, "WI": 5686986, "WV": 1852994, "WY": 563626,
}


def week_anchor(dates: pd.Series) -> pd.Series:
    """把每个日期归入"结束于周五"的那一周，返回该周五（datetime Series）。"""
    delta = (4 - dates.dt.day_of_week) % 7
    return dates + pd.to_timedelta(delta, unit="D")


def _num(v):
    """把 AGSI 字符串值（可能为 '-'/'null'/''）安全转 float，非法返回 NaN。"""
    if v is None:
        return np.nan
    s = str(v).strip()
    if s in {"", "-", "null", "None", "NaN"}:
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


def _read_eia(path: Path, is_weekly: bool) -> pd.DataFrame:
    obj = json.loads(path.read_text(encoding="utf-8"))
    rows = obj["rows"]
    df = pd.DataFrame(rows)
    out = pd.DataFrame()
    if is_weekly:
        out["date"] = pd.to_datetime(df["period"])
    else:
        out["month"] = df["period"].astype(str)
    out["value"] = pd.to_numeric(df["value"], errors="coerce")
    return out


def build_fred() -> pd.DataFrame:
    df = pd.read_csv(FRED_CSV, parse_dates=["observation_date"])
    df["value"] = pd.to_numeric(df["DHHNGSP"], errors="coerce")  # 防御：'.' 等 -> NaN
    df["anchor"] = week_anchor(df["observation_date"])
    w = df.groupby("anchor")["value"].agg(henry_hub_avg="mean", henry_hub_obs_count="count")
    return w.reset_index().rename(columns={"anchor": "week_anchor"})


def build_storage() -> pd.DataFrame:
    df = _read_eia(EIA_STORAGE_JSON, is_weekly=True)
    df = df.rename(columns={"value": "storage_bcf"}).set_index("date")
    return df[["storage_bcf"]]


def _monthly_flatten(axis: pd.DatetimeIndex, monthly: pd.DataFrame, value_col: str,
                     month_col: str, flag_col: str) -> pd.DataFrame:
    """月频 -> 周频平铺折算：当月值赋给该月内所有周五锚点。"""
    by_month = monthly.set_index("month")["value"].rename(value_col)
    month_key = axis.strftime("%Y-%m")
    out = pd.DataFrame({
        "week_anchor": axis,
        month_col: month_key,
        value_col: month_key.map(by_month),
    })
    out[flag_col] = np.where(out[value_col].notna(), 1, np.nan)
    return out


def build_hdd() -> tuple[pd.DataFrame, pd.DataFrame]:
    hist = pd.read_csv(NOAA_HIST_CSV)
    latest = pd.read_csv(NOAA_LATEST_CSV)
    raw = pd.concat([hist, latest], ignore_index=True)
    raw["date"] = pd.to_datetime(raw["date"].astype(str), format="%Y%m%d")
    raw = raw[raw["region"].isin(POP_2010.keys())]  # 仅 48 个 CONUS 州
    raw["hdd"] = pd.to_numeric(raw["hdd"], errors="coerce")
    raw["hdd"] = raw["hdd"].replace(-999.0, np.nan)  # -999 -> NaN（防御，本批未触发）

    wide = raw.pivot_table(index="date", columns="region", values="hdd")
    weights = pd.Series(POP_2010)

    def weighted_mean(row: pd.Series) -> float:
        mask = row.notna()
        if mask.sum() == 0:
            return np.nan
        w = weights[row.index[mask]]
        return float((row[mask] * w).sum() / w.sum())

    conus_daily = wide.apply(weighted_mean, axis=1).rename("hdd_conus_daily")
    daily = conus_daily.reset_index()  # date, hdd_conus_daily

    conus = conus_daily.to_frame()
    conus["anchor"] = week_anchor(conus.index.to_series())
    weekly = conus.groupby("anchor")["hdd_conus_daily"].agg(
        hdd_conus_weekly_sum="sum", hdd_obs_count="count"
    ).reset_index().rename(columns={"anchor": "week_anchor"})
    return daily, weekly


def build_gie() -> tuple[pd.DataFrame, pd.DataFrame]:
    """从 per-country 原始数据重建正确的欧盟聚合（修复 P1 的 Σwgv/Σ(wgv/full) 缺陷）。

    - workingGasVolume = 工作气容量（TWh）；gasInStorage = 当前库存（TWh）。
    - 正确充满度 = Σ gasInStorage / Σ workingGasVolume × 100（容量加权）。
    """
    obj = json.loads(GIE_JSON.read_text(encoding="utf-8"))
    rbc = obj.get("rows_by_country", {})
    flat = []
    for cc, items in rbc.items():
        for it in items:
            flat.append({
                "date": it.get("gasDayStart"),
                "capacity_twh": _num(it.get("workingGasVolume")),
                "inventory_twh": _num(it.get("gasInStorage")),
            })
    df = pd.DataFrame(flat)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    daily = df.groupby("date").agg(
        capacity_twh=("capacity_twh", "sum"),
        inventory_twh=("inventory_twh", "sum"),
        countries_reported=("capacity_twh", "count"),
    ).reset_index()
    daily["full_pct"] = np.where(
        daily["capacity_twh"] > 0,
        daily["inventory_twh"] / daily["capacity_twh"] * 100.0,
        np.nan,
    )
    daily = daily.sort_values("date")
    daily["anchor"] = week_anchor(daily["date"])
    weekly = daily.groupby("anchor").agg(
        gie_full_pct=("full_pct", "last"),
        gie_wgv_twh=("capacity_twh", "last"),
        gie_in_storage_twh=("inventory_twh", "last"),
        gie_countries_reported=("countries_reported", "last"),
    ).reset_index().rename(columns={"anchor": "week_anchor"})
    return daily, weekly


def main() -> None:
    PROC.mkdir(parents=True, exist_ok=True)

    fred_weekly = build_fred()
    storage_weekly = build_storage()
    hdd_daily, hdd_weekly = build_hdd()
    gie_daily, gie_weekly = build_gie()

    # 主表周频轴：2010-01-01（首个周五，storage/HDD/月频起点）至 2026-11-13（最后一个周五）
    axis = pd.date_range("2010-01-01", "2026-11-13", freq="W-FRI")
    axis_s = axis.strftime("%Y-%m-%d")

    prod_monthly = _read_eia(EIA_PROD_JSON, is_weekly=False)
    lng_monthly = _read_eia(EIA_LNG_JSON, is_weekly=False)
    prod_weekly = _monthly_flatten(axis, prod_monthly, "production_mmcf",
                                   "production_month", "production_is_monthly_flattened")
    lng_weekly = _monthly_flatten(axis, lng_monthly, "lng_export_mmcf",
                                  "lng_export_month", "lng_export_is_monthly_flattened")

    master = pd.DataFrame({"week_anchor": axis})
    master = master.merge(fred_weekly, on="week_anchor", how="left")
    master = master.merge(storage_weekly, left_on="week_anchor", right_index=True, how="left")
    master = master.merge(prod_weekly, on="week_anchor", how="left")
    master = master.merge(lng_weekly, on="week_anchor", how="left")
    master = master.merge(hdd_weekly, on="week_anchor", how="left")
    master = master.merge(gie_weekly, on="week_anchor", how="left")

    # 列顺序（主表口径）
    col_order = [
        "week_anchor",
        "henry_hub_avg", "henry_hub_obs_count",
        "storage_bcf",
        "production_mmcf", "production_month", "production_is_monthly_flattened",
        "lng_export_mmcf", "lng_export_month", "lng_export_is_monthly_flattened",
        "hdd_conus_weekly_sum", "hdd_obs_count",
        "gie_full_pct", "gie_wgv_twh", "gie_in_storage_twh", "gie_countries_reported",
    ]
    master = master[col_order]
    master["week_anchor"] = master["week_anchor"].dt.strftime("%Y-%m-%d")

    # 中间构件
    fred_weekly["week_anchor"] = fred_weekly["week_anchor"].dt.strftime("%Y-%m-%d")
    storage_out = storage_weekly.reset_index()
    storage_out["date"] = storage_out["date"].dt.strftime("%Y-%m-%d")
    prod_weekly["week_anchor"] = prod_weekly["week_anchor"].dt.strftime("%Y-%m-%d")
    lng_weekly["week_anchor"] = lng_weekly["week_anchor"].dt.strftime("%Y-%m-%d")
    hdd_daily["date"] = hdd_daily["date"].dt.strftime("%Y-%m-%d")
    hdd_weekly["week_anchor"] = hdd_weekly["week_anchor"].dt.strftime("%Y-%m-%d")
    gie_daily["date"] = gie_daily["date"].dt.strftime("%Y-%m-%d")
    gie_weekly["week_anchor"] = gie_weekly["week_anchor"].dt.strftime("%Y-%m-%d")

    master.to_csv(PROC / "weekly_master.csv", index=False)
    fred_weekly.to_csv(PROC / "henry_hub_weekly.csv", index=False)
    storage_out.to_csv(PROC / "eia_storage_weekly.csv", index=False)
    prod_weekly.to_csv(PROC / "eia_production_weekly.csv", index=False)
    lng_weekly.to_csv(PROC / "eia_lng_export_weekly.csv", index=False)
    hdd_daily.to_csv(PROC / "noaa_hdd_conus_daily.csv", index=False)
    hdd_weekly.to_csv(PROC / "noaa_hdd_weekly.csv", index=False)
    gie_daily[["date", "full_pct", "capacity_twh", "inventory_twh", "countries_reported"]].to_csv(
        PROC / "gie_agsi_eu_daily_corrected.csv", index=False)
    gie_weekly.to_csv(PROC / "gie_agsi_eu_weekly.csv", index=False)

    # 汇总打印（供清洗日志核对）
    print("== P2 build_weekly 汇总 ==")
    print(f"周频主表行数（周五）: {len(master)}  范围 {master['week_anchor'].iloc[0]} ~ {master['week_anchor'].iloc[-1]}")
    for c in ["henry_hub_avg", "storage_bcf", "production_mmcf", "lng_export_mmcf",
              "hdd_conus_weekly_sum", "gie_full_pct", "gie_wgv_twh", "gie_in_storage_twh"]:
        nonnull = master[c].notna().sum()
        print(f"  {c:26s} 非空 {nonnull:5d}  缺失 {len(master)-nonnull:5d}")
    print(f"production 月频折算标记(1) 数量: {(master['production_is_monthly_flattened']==1).sum()}")
    print(f"lng_export 月频折算标记(1) 数量: {(master['lng_export_is_monthly_flattened']==1).sum()}")
    print(f"FRED 周平均(全历史 1997+) 行数: {len(fred_weekly)}")
    print(f"CONUS 人口加权 HDD 日频行数: {len(hdd_daily)}")


if __name__ == "__main__":
    main()
