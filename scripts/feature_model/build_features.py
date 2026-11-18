# -*- coding: utf-8 -*-
"""
P3 · L1+L2 指标与特征工程（可复现）。

输入：data/processed/weekly_master.csv（P2 周频对齐主表，只读）。
输出（data/processed/）：
- supply_demand_monthly.csv    # L1 月度供需平衡表（表观口径）
- storage_deviation_weekly.csv # L1 库存偏离度 SD（描述性，基于当周库存）
- features_weekly.csv          # L2 特征表（4 因子 + 标签 + as-of 元数据）

as-of 防未来信息铁律：
- SD 前5年同周基线只用 Y-5..Y-1（不含当年）；
- HDD/供给/EU 的 10 年同周基线只用历史年份（最多 10 年、至少 3 个有效值）；
- 月频因子（产量/LNG）进特征 shift 3 个月（13 周）；周储气进特征 shift 1 周（周四发布）；
- 标签用未来价格，仅作 y，绝不进特征。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PROC = ROOT / "data" / "processed"
MASTER = PROC / "weekly_master.csv"

SD_LOOKBACK_YEARS = 5   # 库存偏离度基线：前 5 年
LONG_LOOKBACK_YEARS = 10  # HDD/供给/EU 基线：最多前 10 年
MIN_VALID = 5           # 基线最少有效样本数（SD 需满 5 年；10 年基线至少 5 年，早样本近似）
MONTHLY_SHIFT_WEEKS = 13  # 月频 shift 3 个月（13 周，保守于实测 2.5 月）
STORAGE_SHIFT_WEEKS = 1   # 周储气 shift 1 周（周四发布）


def prior_sameweek_stats(df: pd.DataFrame, col: str, week: int, year: int,
                         lookback: int, min_n: int = MIN_VALID):
    vals = df.loc[(df["isoweek"] == week) & (df["year"] >= year - lookback)
                  & (df["year"] < year), col].dropna()
    if len(vals) < min_n:
        return np.nan, np.nan
    return float(vals.mean()), float(vals.std(ddof=0))


def build_monthly_balance(m: pd.DataFrame) -> pd.DataFrame:
    """L1 月度供需平衡表（表观口径，近似）。"""
    prod = m.dropna(subset=["production_mmcf"]).groupby("production_month")["production_mmcf"].first()
    lng = m.dropna(subset=["lng_export_mmcf"]).groupby("lng_export_month")["lng_export_mmcf"].first()

    ms = m.dropna(subset=["storage_bcf"]).copy()
    ms["month"] = ms["week_anchor"].dt.to_period("M").astype(str)
    storage_end = ms.groupby("month")["storage_bcf"].last()  # 月末库存（BCF）
    storage_change_bcf = storage_end.diff()  # 净注入(+)/采出(-)，BCF

    df = pd.DataFrame({"month": sorted(set(prod.index) | set(lng.index))})
    df = df.merge(prod.rename("production_mmcf"), left_on="month", right_index=True, how="left")
    df = df.merge(lng.rename("lng_export_mmcf"), left_on="month", right_index=True, how="left")
    df = df.merge(storage_change_bcf.rename("storage_change_bcf"), left_on="month", right_index=True, how="left")

    df["storage_change_mmcf"] = df["storage_change_bcf"] * 1000.0  # BCF -> MMCF
    df["apparent_supply_mmcf"] = df["production_mmcf"]  # 表观供应 = 干气产量（近似，缺管输净进口）
    # 表观需求 = 产量 - 库存变化 - LNG出口（残差 = 国内消费 + 管输净出口 - 净进口，近似）
    df["apparent_demand_mmcf"] = (
        df["production_mmcf"] - df["storage_change_mmcf"] - df["lng_export_mmcf"]
    )
    df["gap_rate"] = (df["apparent_demand_mmcf"] - df["apparent_supply_mmcf"]) / df["apparent_supply_mmcf"]
    return df


def build_sd(m: pd.DataFrame) -> pd.DataFrame:
    """L1 库存偏离度 SD（描述性，基于当周库存；特征侧再 shift 1 周）。"""
    rows = []
    for _, r in m.iterrows():
        mu, sig = prior_sameweek_stats(m, "storage_bcf", int(r["isoweek"]), int(r["year"]), SD_LOOKBACK_YEARS)
        sd = (r["storage_bcf"] - mu) / sig if (sig and sig > 0 and not np.isnan(mu)) else np.nan
        rows.append({"week_anchor": r["week_anchor"], "storage_bcf": r["storage_bcf"],
                     "sd_5yr_mean": mu, "sd_5yr_std": sig, "sd": sd})
    return pd.DataFrame(rows)


def build_features(m: pd.DataFrame, sd_df: pd.DataFrame) -> pd.DataFrame:
    """L2 特征表（4 因子，as-of 口径）。"""
    df = m.copy()
    df = df.merge(sd_df[["week_anchor", "sd"]], on="week_anchor", how="left")

    # --- 因子1：SD（库存偏离度，shift 1 周：用上周库存的偏离度）---
    df["sd_feature"] = df["sd"].shift(STORAGE_SHIFT_WEEKS)

    # --- HDD 4 周滚动累加 ---
    df["hdd_4wk_sum"] = df["hdd_conus_weekly_sum"].rolling(4, min_periods=1).sum()

    # --- 月频 shift 3 个月后的供给 4 周均值 ---
    df["production_shift13"] = df["production_mmcf"].shift(MONTHLY_SHIFT_WEEKS)
    df["production_month_used"] = df["production_month"].shift(MONTHLY_SHIFT_WEEKS)
    df["supply_4wk_avg"] = df["production_shift13"].rolling(4, min_periods=1).mean()

    # --- 计算各因子相对历史同周的异常（只用到历史年份，防未来信息）---
    hdd_anom, eu_dev = [], []
    for _, r in df.iterrows():
        wk, yr = int(r["isoweek"]), int(r["year"])
        # HDD 4周异常：近4周累加 - 历史同周均值
        mu_h, _ = prior_sameweek_stats(df, "hdd_4wk_sum", wk, yr, LONG_LOOKBACK_YEARS)
        hdd_anom.append(r["hdd_4wk_sum"] - mu_h if not np.isnan(mu_h) else np.nan)
        # 欧盟储气代理因子：gie_full_pct - 历史同周均值
        mu_e, _ = prior_sameweek_stats(df, "gie_full_pct", wk, yr, LONG_LOOKBACK_YEARS)
        eu_dev.append(r["gie_full_pct"] - mu_e if not np.isnan(mu_e) else np.nan)

    df["hdd_anomaly_4wk"] = hdd_anom
    df["eu_storage_deviation"] = eu_dev

    # 供给冲击 4 周 Z：shift 后供给近4周均值相对其自身 trailing 52 周均值/标准差 的滚动 Z。
    # 注：干气产量存在强长期上升趋势，"相对10年同窗口均值"会被趋势主导（几乎恒正），
    # 故改用 trailing 52 周滚动 Z 以捕捉真实供给冲击（冻产/检修/突发增减），去趋势。
    roll_mean = df["supply_4wk_avg"].rolling(52, min_periods=26).mean().shift(1)
    roll_std = df["supply_4wk_avg"].rolling(52, min_periods=26).std(ddof=0).shift(1)
    df["supply_shock_4wk_z"] = np.where(
        roll_std > 0, (df["supply_4wk_avg"] - roll_mean) / roll_std, np.nan
    )

    # --- 标签：未来 h 周 Henry Hub 周均值涨跌方向（0/1，只作 y）---
    for h in (1, 2, 3, 4):
        df[f"label_h{h}"] = (df["henry_hub_avg"].shift(-h) > df["henry_hub_avg"]).astype(float)
        df.loc[df["henry_hub_avg"].shift(-h).isna(), f"label_h{h}"] = np.nan

    # --- 输出列 ---
    out_cols = [
        "week_anchor", "year", "isoweek",
        "henry_hub_avg",
        "storage_bcf", "storage_shift1",
        "sd", "sd_feature",
        "hdd_conus_weekly_sum", "hdd_4wk_sum", "hdd_anomaly_4wk",
        "production_mmcf", "production_shift13", "production_month_used",
        "supply_4wk_avg", "supply_shock_4wk_z",
        "gie_full_pct", "eu_storage_deviation",
        "label_h1", "label_h2", "label_h3", "label_h4",
    ]
    df["storage_shift1"] = df["storage_bcf"].shift(STORAGE_SHIFT_WEEKS)
    df = df[out_cols]
    df["week_anchor"] = df["week_anchor"].dt.strftime("%Y-%m-%d")
    return df


def main() -> None:
    m = pd.read_csv(MASTER, parse_dates=["week_anchor"]).sort_values("week_anchor").reset_index(drop=True)
    m["year"] = m["week_anchor"].dt.year
    m["isoweek"] = m["week_anchor"].dt.isocalendar().week.astype(int)

    balance = build_monthly_balance(m)
    sd_df = build_sd(m)
    features = build_features(m, sd_df)

    balance.to_csv(PROC / "supply_demand_monthly.csv", index=False)
    sd_df["week_anchor"] = sd_df["week_anchor"].dt.strftime("%Y-%m-%d")
    sd_df.to_csv(PROC / "storage_deviation_weekly.csv", index=False)
    features.to_csv(PROC / "features_weekly.csv", index=False)

    # 汇总
    print("== P3 build_features 汇总 ==")
    print(f"月度供需平衡表: {len(balance)} 月  {balance['month'].iloc[0]} ~ {balance['month'].iloc[-1]}")
    print(f"SD 周频: {len(sd_df)} 周，SD 非空 {sd_df['sd'].notna().sum()}")
    print(f"特征表: {len(features)} 周  {features['week_anchor'].iloc[0]} ~ {features['week_anchor'].iloc[-1]}")
    for c in ["sd_feature", "hdd_anomaly_4wk", "supply_shock_4wk_z", "eu_storage_deviation", "label_h1", "label_h4"]:
        print(f"  {c:24s} 非空 {features[c].notna().sum():5d}")


if __name__ == "__main__":
    main()
