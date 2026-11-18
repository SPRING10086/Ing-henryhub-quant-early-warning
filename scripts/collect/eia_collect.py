# -*- coding: utf-8 -*-
"""
EIA v2 采集（P1 补跑，key 已配置）：

- 三个源的 series 代码已经 v2 元数据/facets 端点确认后落库：
  1) 周储气  natural-gas/stor/wkly   NW2_EPG0_SWO_R48_BCF  (weekly, Lower 48 working gas, BCF)
  2) 周产量（月度源）natural-gas/prod/sum  N9070US2  (monthly, U.S. Dry Production, MMcf)
  3) LNG 周出口（月度源）natural-gas/move/poe2  N9133US2  (monthly, Liquefied U.S. LNG Exports, MMcf)

- 重要事实：EIA v2 无「周频产量」与「周频 LNG 出口」路由（prod/wkly、lng/exports/wkly 均 400/404，
  全部 natural-gas 子路由中仅 stor/wkly 为 weekly）。产量与 LNG 出口按月度采集（唯一可用频率），
  并在合并总表中按月值前向填充到周频（填充逻辑留痕，见 manifest note）。

- 请求间隔 >=2s（免费 key 限速）；分页拉取；原始 JSON 带时间戳落盘不覆盖；
- 派生周频 CSV 与合并总表落 data/processed/（稳定文件名，属派生工件非 raw）。
"""
from __future__ import annotations

import csv
import json
from bisect import bisect_right
from datetime import datetime, timezone

import common

PROCESSED = common.ROOT / "data" / "processed"
BASE = "https://api.eia.gov/v2"

SOURCES = [
    {
        "key": "eia_storage_wkly",
        "route": "natural-gas/stor/wkly",
        "series": "NW2_EPG0_SWO_R48_BCF",
        "freq": "weekly",
        "start": "2010-01-01",
        "end": "2026-11-06",
        "csv": "storage_weekly.csv",
        "label": "Weekly Lower 48 Working Gas Underground Storage",
    },
    {
        "key": "eia_drygas_prod_monthly",
        "route": "natural-gas/prod/sum",
        "series": "N9070US2",
        "freq": "monthly",
        "start": "2010-01",
        "end": "2026-08",
        "csv": "production_weekly.csv",
        "label": "U.S. Dry Natural Gas Production (monthly, ffilled to weekly)",
    },
    {
        "key": "eia_lng_exports_monthly",
        "route": "natural-gas/move/poe2",
        "series": "N9133US2",
        "freq": "monthly",
        "start": "2010-01",
        "end": "2026-08",
        "csv": "lng_export_weekly.csv",
        "label": "Liquefied U.S. Natural Gas Exports (monthly, ffilled to weekly)",
    },
]


def _num(v):
    if v is None:
        return None
    s = str(v).strip()
    if s in {"", ".", "null", "None", "NaN", "nan"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_series(src: dict, api_key: str) -> list[dict]:
    url = f"{BASE}/{src['route']}/data/"
    rows: list[dict] = []
    offset = 0
    length = 5000
    total = None
    while True:
        params = {
            "api_key": api_key,
            "frequency": src["freq"],
            "data[0]": "value",
            "facets[series][]": src["series"],
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "start": src["start"],
            "end": src["end"],
            "offset": offset,
            "length": length,
        }
        resp = common.http_get(url, params=params, interval=2.0, timeout=90)
        body = resp.json().get("response", {})
        data = body.get("data", [])
        rows.extend(data)
        if total is None:
            total = int(body.get("total", 0))
        offset += len(data)
        if not data or offset >= total:
            break
    return rows


def _period_to_date(period: str) -> str:
    """月频 period 形如 'YYYY-MM' → 'YYYY-MM-01'；周频 'YYYY-MM-DD' 原样返回。"""
    p = period.strip()
    if len(p) == 7:  # YYYY-MM
        return p + "-01"
    return p


def _ffill(monthly: list[tuple[str, float | None]], weekly_dates: list[str]) -> dict[str, float | None]:
    """按月值前向填充到周频日期：每个周频日期取 <= 该日期的最近月度值。"""
    mkeys = [d for d, _ in monthly]
    mvals = [v for _, v in monthly]
    out: dict[str, float | None] = {}
    for wd in weekly_dates:
        i = bisect_right(mkeys, wd) - 1
        out[wd] = mvals[i] if i >= 0 else None
    return out


def _write_csv(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])


def collect() -> list[dict]:
    PROCESSED.mkdir(parents=True, exist_ok=True)
    api_key = common.env_key_any("EIA_API_KEY", "EIA_KEY")
    if not api_key:
        return [
            common.result_record(source=s["key"], status="skipped_no_key", files=[], note="未检测到 EIA_API_KEY/EIA_KEY")
            for s in SOURCES
        ]

    results: list[dict] = []
    series_rows: dict[str, list[dict]] = {}
    for src in SOURCES:
        try:
            rows = fetch_series(src, api_key)
            series_rows[src["key"]] = rows
            # 原始 JSON（带时间戳，不覆盖）
            obj = {
                "route": src["route"],
                "frequency": src["freq"],
                "series": src["series"],
                "series_label": src["label"],
                "period_start": src["start"],
                "period_end": src["end"],
                "rows": rows,
            }
            path = common.save_json(src["key"], ".json", obj)
            results.append(
                common.result_record(
                    source=src["key"],
                    status="ok",
                    files=[path.name],
                    note=f"series 已确认 {src['series']}（{src['label']}），{src['freq']}，{len(rows)} 行。",
                )
            )
        except Exception as exc:  # noqa: BLE001
            common.log.exception("EIA 采集失败: %s", src["key"])
            results.append(common.result_record(source=src["key"], status="error", files=[], note=f"采集失败: {exc}"))

    # ---- 派生周频 CSV 与合并总表 ----
    try:
        build_weekly_outputs(series_rows)
    except Exception as exc:  # noqa: BLE001
        common.log.exception("构建周频 CSV/合并表失败")
        results.append(common.result_record(source="eia_weekly_outputs", status="error", files=[], note=f"派生 CSV 构建失败: {exc}"))

    return results


def build_weekly_outputs(series_rows: dict[str, list[dict]]) -> None:
    """产出 storage_weekly.csv / production_weekly.csv / lng_export_weekly.csv 与 merged_weekly.csv。"""
    def to_date_value(key: str):
        out: list[tuple[str, float | None]] = []
        for r in series_rows.get(key, []):
            out.append((_period_to_date(r.get("period", "")), _num(r.get("value"))))
        out.sort(key=lambda x: x[0])
        return out

    storage = to_date_value("eia_storage_wkly")
    production_monthly = to_date_value("eia_drygas_prod_monthly")
    lng_monthly = to_date_value("eia_lng_exports_monthly")

    weekly_dates = [d for d, _ in storage]
    prod_ff = _ffill(production_monthly, weekly_dates)
    lng_ff = _ffill(lng_monthly, weekly_dates)

    _write_csv(PROCESSED / "storage_weekly.csv", ["date", "storage_bcf"], storage)
    _write_csv(PROCESSED / "production_weekly.csv", ["date", "production_mmcf"], [(d, prod_ff[d]) for d in weekly_dates])
    _write_csv(PROCESSED / "lng_export_weekly.csv", ["date", "lng_export_mmcf"], [(d, lng_ff[d]) for d in weekly_dates])
    _write_csv(
        PROCESSED / "merged_weekly.csv",
        ["date", "storage", "production", "lng_export"],
        [(d, dict(storage).get(d), prod_ff[d], lng_ff[d]) for d in weekly_dates],
    )
    common.log.info(
        "周频 CSV 已落 data/processed/：storage_weekly.csv（%d 周）、production_weekly.csv、lng_export_weekly.csv、merged_weekly.csv",
        len(weekly_dates),
    )


if __name__ == "__main__":
    for rec in collect():
        print(json.dumps(rec, ensure_ascii=False))
