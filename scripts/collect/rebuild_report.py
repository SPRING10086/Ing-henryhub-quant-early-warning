# -*- coding: utf-8 -*-
"""
P1 元数据刷新器：扫描 data/raw 现有落盘文件，重建 manifest.json 与缺失率统计表。

- 不重新发起采集、不覆盖任何 raw 文件；
- 对每个源按「最新时间戳组」挑选落盘文件（raw 只追加，旧时间戳文件保留但统计以最新为准）；
- 无 key 且无落盘的源（EIA/GIE）在 manifest 与统计表中标注「待 key 补跑」；
- 供 noaa_hdd_history.py 落盘后调用，也可单独运行。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import common
import missing_report

SOURCES = [
    {
        "source": "fred_dhhngsp",
        "prefix": "fred_dhhngsp",
        "endpoint": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DHHNGSP",
        "needs_key": False,
        "key_env": None,
        "series_hypothesis": None,
        "note": "Henry Hub 现货，CSV 直下，缺失以空串表示。",
    },
    {
        "source": "eia_storage_wkly",
        "prefix": "eia_storage_wkly",
        "endpoint": "https://api.eia.gov/v2/natural-gas/stor/wkly/data/",
        "needs_key": True,
        "key_envs": ["EIA_API_KEY", "EIA_KEY"],
        "series_hypothesis": ["NW2_EPG0_SWO_R48_BCF"],
        "series_confirmed": True,
        "note": "周储气（Lower 48 working gas, BCF），series 已 facets 确认。",
    },
    {
        "source": "eia_drygas_prod_monthly",
        "prefix": "eia_drygas_prod_monthly",
        "endpoint": "https://api.eia.gov/v2/natural-gas/prod/sum/data/",
        "needs_key": True,
        "key_envs": ["EIA_API_KEY", "EIA_KEY"],
        "series_hypothesis": ["N9070US2"],
        "series_confirmed": True,
        "note": "U.S. Dry Production（MMcf）——EIA v2 仅月度，无周频产量路由；合并表按月值前向填充到周频。",
    },
    {
        "source": "eia_lng_exports_monthly",
        "prefix": "eia_lng_exports_monthly",
        "endpoint": "https://api.eia.gov/v2/natural-gas/move/poe2/data/",
        "needs_key": True,
        "key_envs": ["EIA_API_KEY", "EIA_KEY"],
        "series_hypothesis": ["N9133US2"],
        "series_confirmed": True,
        "note": "Liquefied U.S. LNG Exports（MMcf）——EIA v2 仅月度，无周频 LNG 路由；合并表按月值前向填充到周频。",
    },
    {
        "source": "noaa_hdd_states",
        "prefix": "noaa_hdd_states",
        "exclude": "_hist",
        "endpoint": "https://ftp.cpc.ncep.noaa.gov/htdocs/degree_days/weighted/daily_data/latest/StatesCONUS.Heating.txt",
        "needs_key": False,
        "key_env": None,
        "series_hypothesis": None,
        "note": "人口加权 HDD（按州 + CONUS）当前年（latest 端点）。",
    },
    {
        "source": "noaa_hdd_states_hist",
        "prefix": "noaa_hdd_states_hist",
        "endpoint": "https://ftp.cpc.ncep.noaa.gov/htdocs/degree_days/weighted/daily_data/<year>/StatesCONUS.Heating.txt",
        "needs_key": False,
        "key_env": None,
        "series_hypothesis": None,
        "note": "人口加权 HDD 历史（2010 至今，逐年归档拼接），供 10 年均值 / 异常基线。",
    },
    {
        "source": "gie_agsi_eu",
        "prefix": "gie_agsi_eu",
        "endpoint": "https://agsi.gie.eu/api",
        "needs_key": True,
        "key_envs": ["GIE_AGSI_API_KEY", "GIE_AGSI_KEY"],
        "series_hypothesis": None,
        "series_confirmed": False,
        "note": "欧盟储气充满度（TTF 代理）。country=eu 直取不可用，改为欧洲 20 国体积加权聚合；本地缓存游标 + 仅增量拉取。",
    },
]


def _latest_files(spec: dict) -> list[str]:
    """返回该源最新时间戳组的落盘文件名列表（basename，表格文件优先）。"""
    prefix = spec["prefix"]
    exclude = spec.get("exclude")
    by_ts: dict[str, list[str]] = {}
    for p in common.RAW_DIR.iterdir():
        if not p.is_file():
            continue
        name = p.name
        if not name.startswith(prefix + "_"):
            continue
        if exclude and exclude in name:
            continue
        rest = name[len(prefix) + 1:]
        ts = rest.rsplit(".", 1)[0]
        by_ts.setdefault(ts, []).append(name)
    if not by_ts:
        return []
    latest = max(by_ts.keys())
    files = by_ts[latest]
    # 表格文件（csv/json）排在原始文本（txt）前，便于统计输出
    files.sort(key=lambda f: (0 if f.rsplit(".", 1)[-1].lower() in {"csv", "json"} else 1, f))
    return files


def _key_note(spec: dict) -> str:
    names = spec.get("key_envs") or ["KEY"]
    return (
        f"待 key 补跑：未检测到环境变量 {'/'.join(names)}。"
        "脚本已含 env 加载 + 元数据确认 series + 分页拉取 + 重试退避，key 到位后直接运行。"
    )


def rebuild() -> dict:
    results: list[dict] = []
    for spec in SOURCES:
        files = _latest_files(spec)
        if files:
            results.append(
                {
                    "source": spec["source"],
                    "status": "ok",
                    "files": files,
                    "note": spec["note"],
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        elif spec.get("needs_key"):
            results.append(
                {
                    "source": spec["source"],
                    "status": "skipped_no_key",
                    "files": [],
                    "note": _key_note(spec),
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        else:
            results.append(
                {
                    "source": spec["source"],
                    "status": "error",
                    "files": [],
                    "note": "无落盘文件（采集未成功）。",
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                }
            )

    # manifest（稳定文件，覆盖为预期行为）
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": "lng-trade-research P1 数据采集",
        "sources": {},
        "results": results,
    }
    for spec in SOURCES:
        manifest["sources"][spec["source"]] = {
            "endpoint": spec["endpoint"],
            "api_key_required": spec.get("needs_key", False),
            "key_configured": bool(common.env_key_any(*(spec.get("key_envs") or []))) if spec.get("needs_key") else False,
            "series_hypothesis": spec.get("series_hypothesis"),
            "series_confirmed": bool(spec.get("series_confirmed", False)),
            "note": spec["note"],
        }
    (common.COLLECT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    common.log.info("manifest 已刷新: manifest.json")

    # 缺失率统计表
    table = missing_report.generate(results)
    csv_path, md_path = missing_report.write_reports(table)
    common.log.info("缺失率统计表已刷新: %s / %s", csv_path.name, md_path.name)

    return manifest


if __name__ == "__main__":
    import json as _json

    m = rebuild()
    print(_json.dumps({"generated_at": m["generated_at"], "sources": list(m["sources"]), "results": m["results"]}, ensure_ascii=False, indent=2))
