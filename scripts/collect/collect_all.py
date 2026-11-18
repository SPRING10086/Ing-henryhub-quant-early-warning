# -*- coding: utf-8 -*-
"""
P1 数据采集总入口（按优先级 FRED > EIA > NOAA > GIE-AGSI 顺序执行）。

- 每个采集源独立优雅降级：单源失败/缺 key 不中断整体；
- 汇总各源结果，写 manifest（scripts/collect/manifest.json，记录端点与 series 代码）与
  运行日志（data/raw/collect_run_<ts>.json）；
- 最后生成缺失率统计表（data/raw/missing_report_<ts>.csv/.md）。

用法：python scripts/collect/collect_all.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import common
import fred_hh
import eia_collect
import noaa_hdd
import noaa_hdd_history
import gie_agsi
import missing_report

MANIFEST_PATH = common.COLLECT_DIR / "manifest.json"


def build_manifest(results: list[dict]) -> dict:
    eia_key = bool(common.env_key("EIA_API_KEY"))
    gie_key = bool(common.env_key("GIE_AGSI_API_KEY"))
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": "lng-trade-research P1 数据采集",
        "sources": {
            "fred_dhhngsp": {
                "endpoint": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DHHNGSP",
                "api_key_required": False,
                "note": "Henry Hub 现货，CSV 直下，缺失以空串表示。",
            },
            "eia_storage_wkly": {
                "endpoint": "https://api.eia.gov/v2/natural-gas/stor/wkly/data/",
                "api_key_required": True,
                "key_configured": eia_key,
                "series_hypothesis": ["NGMEPP0"],
                "series_confirmed": False,
                "note": "series 代码须 key 到位后用 v2 元数据 facets 端点最终确认。",
            },
            "eia_drygas_prod_monthly": {
                "endpoint": "https://api.eia.gov/v2/natural-gas/prod/sum/data/",
                "api_key_required": True,
                "key_configured": eia_key,
                "series_hypothesis": ["N9070US2"],
                "series_confirmed": False,
                "note": "series 代码须 key 到位后用 v2 元数据 facets 端点最终确认。",
            },
            "eia_lng_exports_monthly": {
                "endpoint": "https://api.eia.gov/v2/natural-gas/move/expc/data/",
                "api_key_required": True,
                "key_configured": eia_key,
                "series_hypothesis": ["N9130US2"],
                "series_confirmed": False,
                "note": "series 代码须 key 到位后用 v2 元数据 facets 端点最终确认。",
            },
            "noaa_hdd_states": {
                "endpoint": "https://ftp.cpc.ncep.noaa.gov/htdocs/degree_days/weighted/daily_data/latest/StatesCONUS.Heating.txt",
                "api_key_required": False,
                "note": "人口加权 HDD（按州 + CONUS）当前年；CPC 已迁移为管道符宽表，-999 等缺失标记解析为 NaN。",
            },
            "noaa_hdd_states_hist": {
                "endpoint": "https://ftp.cpc.ncep.noaa.gov/htdocs/degree_days/weighted/daily_data/<year>/StatesCONUS.Heating.txt",
                "api_key_required": False,
                "note": "人口加权 HDD 历史（2010 至今，逐年归档拼接），供 10 年均值 / 异常基线。",
            },
            "gie_agsi_eu": {
                "endpoint": "https://agsi.gie.eu/api",
                "api_key_required": True,
                "key_configured": gie_key,
                "note": "欧盟储气充满度（TTF 代理），country=eu，本地缓存游标 + 仅增量拉取。",
            },
        },
        "results": results,
    }
    return manifest


def main() -> int:
    common.RAW_DIR.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    # 优先级 FRED > EIA > NOAA > GIE-AGSI
    common.log.info("== 采集顺序：FRED > EIA > NOAA > GIE-AGSI ==")

    common.log.info("[1/4] FRED Henry Hub 现货 DHHNGSP")
    results.append(fred_hh.collect())

    common.log.info("[2/4] EIA 周库存 / 月产 / LNG 出口")
    results.extend(eia_collect.collect())

    common.log.info("[3/4] NOAA CPC 人口加权 HDD（当前年 + 历史）")
    results.append(noaa_hdd.collect())
    results.append(noaa_hdd_history.collect())

    common.log.info("[4/4] GIE-AGSI 欧盟储气充满度")
    results.append(gie_agsi.collect())

    # manifest（稳定文件，覆盖为预期行为；记录端点与 series 代码/确认状态）
    manifest = build_manifest(results)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    common.log.info("manifest 已写: %s", MANIFEST_PATH.name)

    # 运行日志（时间戳，追加不覆盖）
    run_log = common.save_json("collect_run", ".json", {"results": results, "manifest": manifest})

    # 缺失率统计表
    table = missing_report.generate(results)
    csv_path, md_path = missing_report.write_reports(table)

    print("\n===== P1 采集结果汇总 =====")
    for rec in results:
        print(f"- {rec['source']}: {rec['status']} | 文件 {rec['files'] or '无'} | {rec['note']}")
    print("\n缺失率统计表: ", csv_path.name, " / ", md_path.name)
    print("运行日志: ", run_log.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
