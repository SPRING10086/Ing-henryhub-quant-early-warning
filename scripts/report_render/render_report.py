# -*- coding: utf-8 -*-
"""
P5 一键 HTML 研判周报渲染器。

- 只读 data/processed 与 docs，不写 raw、不改任何上游文件；
- 固定五段结构：核心结论｜供需证据｜价格概率｜风险清单｜历史对标；
- 数字一律标注来源字段（data/processed/xxx.csv），可溯源 raw；
- 只输出 P(up) 与 宽松/中性/偏紧，禁价格点位；必含免责声明。

用法：python scripts/report_render/render_report.py
输出：reports/weekly_report_latest.html（+ reports/report_data_latest.json 溯源）
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[2]
PROCESSED = ROOT / "data" / "processed"
DOCS = ROOT / "docs"
REPORTS = ROOT / "reports"
TPL_DIR = ROOT / "scripts" / "report_render"


def _load(name: str) -> list[dict]:
    with (PROCESSED / name).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _f(v):
    """安全转 float，空/缺失返回 None。"""
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _hitrate(rows, dir_key):
    n = len(rows)
    hit = sum(1 for r in rows if r.get(dir_key) == r.get("y_true_h1"))
    return hit / n if n else None


def _scenario_hitrate(rows, flag_key, dir_key="lgbm_dir"):
    sub = [r for r in rows if r.get(flag_key) == "1"]
    if not sub:
        return None
    return sum(1 for r in sub if r.get(dir_key) == r.get("y_true_h1")) / len(sub)


def build_context() -> dict:
    bp = _load("backtest_predictions.csv")
    rk = _load("risk_eval_weekly.csv")
    ft = _load("features_weekly.csv")
    sdm = _load("supply_demand_monthly.csv")
    sdv = _load("storage_deviation_weekly.csv")

    latest_feature = max(ft, key=lambda r: r["week_anchor"])
    latest_model = max(bp, key=lambda r: r["week_anchor"])
    latest_risk = max(rk, key=lambda r: r["week_anchor"])
    latest_sd = max((r for r in sdv if r.get("sd")), key=lambda r: r["week_anchor"])
    latest_month = max((r for r in sdm if r.get("apparent_demand_mmcf")), key=lambda r: r["month"])

    # 三模型方向命中率（247 OOS 周，与 P4 报告口径一致）
    hr = {
        "v1": _hitrate(rk, "v1_dir"),
        "logit": _hitrate(rk, "logit_dir"),
        "lgbm": _hitrate(rk, "lgbm_dir"),
        "inertia": _hitrate(rk, "inertia_dir"),
    }
    scen = {
        "heating": _scenario_hitrate(rk, "is_heating"),
        "non_heating": _scenario_hitrate(rk, "is_non_heating"),
        "shock_2022": _scenario_hitrate(rk, "is_shock_2022"),
        "freeport": _scenario_hitrate(rk, "is_shock_freeport"),
    }

    def pct(x, digits=3):
        return f"{x * 100:.1f}%" if x is not None else "—"

    # 风险标签触发映射（R1 寒潮 / R2 LNG / R3 欧盟低位 / R4 高波动）
    risk_flags = [
        {"id": "R1", "name": "寒潮持续", "yellow": latest_risk.get("flag_cold_y") == "1",
         "red": latest_risk.get("flag_cold_r") == "1"},
        {"id": "R2", "name": "LNG 出口扰动", "yellow": latest_risk.get("flag_lng_y") == "1",
         "red": latest_risk.get("flag_lng_r") == "1"},
        {"id": "R3", "name": "欧盟储气低位", "yellow": latest_risk.get("flag_eu_y") == "1",
         "red": latest_risk.get("flag_eu_r") == "1"},
        {"id": "R4", "name": "高波动区间", "yellow": latest_risk.get("flag_vol_y") == "1",
         "red": latest_risk.get("flag_vol_r") == "1"},
    ]
    active_risks = [r for r in risk_flags if r["yellow"] or r["red"]]

    # 三模型方向一致性
    dirs = [
        latest_risk.get("v1_dir"), latest_risk.get("logit_dir"), latest_risk.get("lgbm_dir"),
    ]
    if len(set(dirs)) == 1:
        consensus = "三模型一致看" + ("多" if dirs[0] == "1" else "空")
    else:
        consensus = "三模型方向分歧"

    return {
        "generated_at": date.today().isoformat(),
        "feature_week": latest_feature["week_anchor"],
        "model_week": latest_model["week_anchor"],
        "henry_hub_avg": _f(latest_feature.get("henry_hub_avg")),
        # 供需证据
        "storage_bcf": _f(latest_sd.get("storage_bcf")),
        "sd": _f(latest_sd.get("sd")),
        "sd_5yr_mean": _f(latest_sd.get("sd_5yr_mean")),
        "sd_5yr_std": _f(latest_sd.get("sd_5yr_std")),
        "hdd_anomaly_4wk": _f(latest_feature.get("hdd_anomaly_4wk")),
        "supply_shock_4wk_z": _f(latest_feature.get("supply_shock_4wk_z")),
        "gie_full_pct": _f(latest_feature.get("gie_full_pct")),
        "eu_storage_deviation": _f(latest_feature.get("eu_storage_deviation")),
        "production_mmcf": _f(latest_feature.get("production_shift13") or latest_feature.get("production_mmcf")),
        "production_month_used": latest_feature.get("production_month_used"),
        # P1-1：lng_export_mmcf 改从 supply_demand_monthly 取数（features_weekly 无此列，此前恒为「—」）
        "lng_export_mmcf": _f(latest_month.get("lng_export_mmcf")),
        # P1-4：储气/SD 取值周（周储气周四发布滞后，取 storage_deviation_weekly 最后非空周）
        "sd_week": latest_sd.get("week_anchor"),
        "month": latest_month["month"],
        "apparent_supply_mmcf": latest_month.get("apparent_supply_mmcf"),
        "apparent_demand_mmcf": latest_month.get("apparent_demand_mmcf"),
        "gap_rate": _f(latest_month.get("gap_rate")),
        "storage_change_bcf": latest_month.get("storage_change_bcf"),
        # 价格概率
        "v1_class": latest_model.get("v1_class"),
        "v1_p_up": _f(latest_model.get("v1_p_up")),
        "logit_p_up": _f(latest_model.get("logit_p_up")),
        "lgbm_p_up": _f(latest_model.get("lgbm_p_up")),
        "consensus": consensus,
        # 风险
        "risk_flags": risk_flags,
        "active_risks": active_risks,
        "eu_dev_z": _f(latest_risk.get("eu_dev_z")),
        "hdd_anom_z": _f(latest_risk.get("hdd_anom_z")),
        "lng_z52": _f(latest_risk.get("lng_z52")) if latest_risk.get("lng_z52") else None,
        "hh_vol8": _f(latest_risk.get("hh_vol8")),
        # 历史对标
        "hr": {k: (pct(v) if v is not None else "—") for k, v in hr.items()},
        "hr_raw": hr,
        "scen": {k: (pct(v) if v is not None else "—") for k, v in scen.items()},
        "n_oos": len(rk),
        "n_folds": len(set(r["fold"] for r in rk)),
        "auc": {"logit": "0.5414", "lgbm": "0.5417"},  # P1-2：AUC 引自 docs/backtest_report.md（硬编码常量，非本脚本计算）
        "v1_weights": {"sd": 0.0, "hdd": 0.0, "supply": 0.8, "eu": 0.2},
    }


def main() -> int:
    ctx = build_context()
    REPORTS.mkdir(parents=True, exist_ok=True)

    env = Environment(loader=FileSystemLoader(str(TPL_DIR)), autoescape=True)
    tpl = env.get_template("template.html")
    html = tpl.render(**ctx)

    out_html = REPORTS / "weekly_report_latest.html"
    out_html.write_text(html, encoding="utf-8")

    # 溯源 JSON（数值快照，便于核对）
    out_json = REPORTS / "report_data_latest.json"
    out_json.write_text(json.dumps(ctx, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"渲染完成：{out_html}")
    print(f"溯源快照：{out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
