# -*- coding: utf-8 -*-
"""
GIE-AGSI 欧盟储气库充满度采集（作为 TTF 代理）。

重要发现：当前 AGSI API 的 country=eu / country=EU 均返回 total=0（EU 聚合已不可直取），
而单个国家（DE/FR/AT…）可正常返回。故本模块改为：
  - 拉取 AGSI「欧洲」国家清单（约 20 国）的逐日 workingGasVolume 与 full；
  - 按日聚合为欧盟（欧洲）充满度：eu_full = Σ workingGasVolume / Σ(workingGasVolume / full)；
  - 原始逐国 JSON 带时间戳落盘不覆盖；派生 EU 日频 CSV 落 data/processed/。

- API Key 仅从环境变量 GIE_AGSI_API_KEY / GIE_AGSI_KEY 只读加载（python-dotenv），禁止硬编码；
- 本地缓存游标 data/raw/.agsi_cache_state.json 记录最近 gasDayStart，仅增量拉取，
  禁止全量反复请求；首次无缓存做有界回填（默认 30 天）；
- 含指数退避重试 + 请求间隔控制。
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

import common

BASE = "https://agsi.gie.eu/api"
CACHE_FILE = common.RAW_DIR / ".agsi_cache_state.json"
PREFIX = "gie_agsi_eu"
PROCESSED = common.ROOT / "data" / "processed"
INITIAL_DAYS = 30
PAGE_SIZE = 300
BACKFILL_START = "2011-01-01"  # AGSI 最早可提供日期（探测：2011-01-01 有数据，2009 无）

# AGSI「欧洲」国家代码（含英国，AGSI 网站 EU 视图口径）
EU_COUNTRIES = [
    "AT", "BE", "BG", "HR", "CZ", "DK", "FR", "DE", "HU", "IE",
    "IT", "LV", "NL", "PL", "PT", "RO", "SK", "ES", "SE", "GB",
]


def _num(v):
    if v is None:
        return None
    s = str(v).strip()
    if s in {"", "-", "null", "None", "NaN"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            common.log.warning("缓存游标损坏，忽略重建")
    return {}


def _save_cache(state: dict) -> None:
    CACHE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_country(cc: str, from_date: str, to_date: str, api_key: str) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while True:
        params = {"country": cc, "from": from_date, "to": to_date, "page": page, "size": PAGE_SIZE}
        resp = common.http_get(BASE, headers={"x-key": api_key}, params=params, interval=1.2, timeout=90)
        payload = resp.json()
        data = payload.get("data", [])
        if not data:
            break
        rows.extend(data)
        if page >= int(payload.get("last_page", page) or page):
            break
        page += 1
    return rows


def _aggregate_eu(rows_by_country: dict[str, list[dict]]) -> dict[str, dict]:
    by_day: dict[str, list[dict]] = {}
    for cc, rows in rows_by_country.items():
        for r in rows:
            day = r.get("gasDayStart")
            if day:
                by_day.setdefault(day, []).append(r)
    agg: dict[str, dict] = {}
    for day, items in by_day.items():
        sum_wgv = 0.0
        sum_cap = 0.0
        for it in items:
            wgv = _num(it.get("workingGasVolume"))
            full = _num(it.get("full"))
            if wgv is None or full is None or full <= 0:
                continue
            sum_wgv += wgv
            sum_cap += wgv / (full / 100.0)
        agg[day] = {
            "eu_full_pct": round(sum_wgv / sum_cap * 100.0, 3) if sum_cap > 0 else None,
            "eu_working_gas_gwh": round(sum_wgv, 1),
            "countries_reported": sum(1 for it in items if _num(it.get("workingGasVolume")) is not None),
        }
    return agg


def collect() -> dict:
    api_key = common.env_key_any("GIE_AGSI_API_KEY", "GIE_AGSI_KEY")
    if not api_key:
        return common.result_record(
            source="gie_agsi_eu",
            status="skipped_no_key",
            files=[],
            note="待 key 补跑：未检测到环境变量 GIE_AGSI_API_KEY/GIE_AGSI_KEY。",
        )

    try:
        cache = _load_cache()
        last_date = cache.get("last_date")
        today = datetime.now(timezone.utc).date()
        if last_date:
            from_date = (datetime.fromisoformat(last_date).date() + timedelta(days=1)).isoformat()
        else:
            from_date = (today - timedelta(days=INITIAL_DAYS)).isoformat()
        to_date = today.isoformat()

        if from_date > to_date:
            return common.result_record(
                source="gie_agsi_eu", status="ok", files=[],
                note=f"缓存已是最新（last_date={last_date}），无新增数据。",
            )

        rows_by_country: dict[str, list[dict]] = {}
        for cc in EU_COUNTRIES:
            rows_by_country[cc] = _fetch_country(cc, from_date, to_date, api_key)
            common.log.info("GIE %s 拉取 %d 行", cc, len(rows_by_country[cc]))

        agg = _aggregate_eu(rows_by_country)
        # 供缺失率统计读取的逐日聚合长表（rows 键）
        agg_rows = [
            {
                "gasDayStart": day,
                "full": r["eu_full_pct"],
                "workingGasVolume": r["eu_working_gas_gwh"],
                "countries_reported": r["countries_reported"],
            }
            for day, r in sorted(agg.items())
        ]
        obj = {
            "note": "country=eu 直取返回 0，本文件为欧洲国家逐日原始数据 + 体积加权欧盟聚合。",
            "from": from_date,
            "to": to_date,
            "countries": EU_COUNTRIES,
            "rows": agg_rows,
            "aggregate": agg,
            "rows_by_country": rows_by_country,
        }
        path = common.save_json(PREFIX, ".json", obj)

        # 派生 EU 日频 CSV
        PROCESSED.mkdir(parents=True, exist_ok=True)
        csv_path = PROCESSED / "gie_agsi_eu_daily.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "eu_full_pct", "eu_working_gas_gwh", "countries_reported"])
            for day in sorted(agg):
                r = agg[day]
                w.writerow([day, "" if r["eu_full_pct"] is None else r["eu_full_pct"],
                            "" if r["eu_working_gas_gwh"] is None else r["eu_working_gas_gwh"],
                            r["countries_reported"]])

        if agg:
            _save_cache({"country": "eu(aggregated)", "last_date": max(agg), "updated_at": common.timestamp()})

        return common.result_record(
            source="gie_agsi_eu",
            status="ok",
            files=[path.name],
            note=(
                f"欧盟聚合（欧洲 {len(EU_COUNTRIES)} 国体积加权）增量拉取 [{from_date}, {to_date}]，"
                f"聚合 {len(agg)} 个 gas day；country=eu 直取不可用已改聚合。"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        common.log.exception("GIE-AGSI 采集失败")
        return common.result_record(source="gie_agsi_eu", status="error", files=[], note=f"采集失败: {exc}")


def backfill(start_date: str = BACKFILL_START) -> dict:
    """一次性全量历史回填：逐国拉取 [start_date, today] 并体积加权聚合；之后仍走增量。"""
    api_key = common.env_key_any("GIE_AGSI_API_KEY", "GIE_AGSI_KEY")
    if not api_key:
        return common.result_record(
            source="gie_agsi_eu", status="skipped_no_key", files=[],
            note="待 key 补跑：未检测到环境变量 GIE_AGSI_API_KEY/GIE_AGSI_KEY。",
        )

    today = datetime.now(timezone.utc).date().isoformat()
    rows_by_country: dict[str, list[dict]] = {}
    for cc in EU_COUNTRIES:
        rows_by_country[cc] = _fetch_country(cc, start_date, today, api_key)
        common.log.info("回填 %s 拉取 %d 行", cc, len(rows_by_country[cc]))

    agg = _aggregate_eu(rows_by_country)
    agg_rows = [
        {
            "gasDayStart": day,
            "full": r["eu_full_pct"],
            "workingGasVolume": r["eu_working_gas_gwh"],
            "countries_reported": r["countries_reported"],
        }
        for day, r in sorted(agg.items())
    ]
    obj = {
        "note": f"全量历史回填（{start_date} 至今），欧洲 20 国体积加权聚合；country=eu 直取不可用。",
        "from": start_date,
        "to": today,
        "countries": EU_COUNTRIES,
        "rows": agg_rows,
        "aggregate": agg,
        "rows_by_country": rows_by_country,
    }
    path = common.save_json(PREFIX, ".json", obj)

    # 覆盖写全量日频 CSV
    PROCESSED.mkdir(parents=True, exist_ok=True)
    csv_path = PROCESSED / "gie_agsi_eu_daily.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "eu_full_pct", "eu_working_gas_gwh", "countries_reported"])
        for day in sorted(agg):
            r = agg[day]
            w.writerow([day, "" if r["eu_full_pct"] is None else r["eu_full_pct"],
                        "" if r["eu_working_gas_gwh"] is None else r["eu_working_gas_gwh"],
                        r["countries_reported"]])

    if agg:
        _save_cache({"country": "eu(aggregated)", "last_date": max(agg), "updated_at": common.timestamp()})

    return common.result_record(
        source="gie_agsi_eu",
        status="ok",
        files=[path.name],
        note=(
            f"全量历史回填 [{start_date}, {today}]：聚合 {len(agg)} 个 gas day，"
            f"覆盖 {sorted(agg)[0] if agg else '-'} ~ {max(agg) if agg else '-'}。"
        ),
    )


if __name__ == "__main__":
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "incremental"
    if mode == "backfill":
        print(json.dumps(backfill(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(collect(), ensure_ascii=False, indent=2))
