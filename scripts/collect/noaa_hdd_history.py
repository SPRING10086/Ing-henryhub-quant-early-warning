# -*- coding: utf-8 -*-
"""
NOAA CPC 历史人口加权采暖度日（HDD）采集（2010 至今，供 P3 异常基线 / 10 年均值）。

- 归档结构：daily_data/ 无全历史合并文件，为逐年归档
  daily_data/<year>/StatesCONUS.Heating.txt（宽表 Region|YYYYMMDD...）；
- 本模块拉取 HIST_START_YEAR..(当前年-1) 各年文件，按年份顺序拼接为一份历史原始文本，
  沿用既有解析（-999/空 等缺失标记 → NaN），产出长表 CSV；
- 原始文本与解析 CSV 均带下载时间戳落盘，绝不覆盖已落盘文件；
- 当前年（2026）已由 noaa_hdd.py 的 latest 端点落盘，历史仅覆盖至上一完整年。

环境变量覆盖：NOAA_HDD_HIST_START（默认 2010）、NOAA_HDD_HIST_END（默认 当前年-1）。
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone

import common
import noaa_hdd

BASE = "https://ftp.cpc.ncep.noaa.gov/htdocs/degree_days/weighted/daily_data"
PREFIX = "noaa_hdd_states_hist"
DEFAULT_START = 2010


def _year_range() -> range:
    current_year = datetime.now(timezone.utc).year
    start = int(os.environ.get("NOAA_HDD_HIST_START") or DEFAULT_START)
    end = int(os.environ.get("NOAA_HDD_HIST_END") or (current_year - 1))
    if end < start:
        raise ValueError(f"历史年份区间非法：start={start} end={end}")
    return range(start, end + 1)


def collect() -> dict:
    years = _year_range()
    blocks = []
    fetched = []
    for y in years:
        url = f"{BASE}/{y}/StatesCONUS.Heating.txt"
        try:
            resp = common.http_get(url, interval=1.2, timeout=90)
            text = resp.content.decode("utf-8", errors="replace")
            if "Region|" not in text:
                raise ValueError(f"{y} 年文件缺少 Region| 头")
            blocks.append(text)
            fetched.append(y)
            common.log.info("历史 %d 年已拉取 (%d chars)", y, len(text))
        except Exception as exc:  # noqa: BLE001
            common.log.warning("历史 %d 年拉取失败，跳过：%s", y, exc)

    if not blocks:
        return common.result_record(
            source="noaa_hdd_states_hist",
            status="error",
            files=[],
            note="历史 HDD 全部年份拉取失败。",
        )

    merged_text = "\n".join(blocks)
    raw_path = common.save_text(PREFIX, ".txt", merged_text)

    # 沿用既有解析（兼容宽表与长表，-999 → NaN）
    parsed = noaa_hdd.parse_noaa(merged_text)
    if parsed is None:
        return common.result_record(
            source="noaa_hdd_states_hist",
            status="error",
            files=[raw_path.name],
            note="原始历史文本已落盘，但解析失败。",
        )
    fields, records = parsed
    csv_path = common.unique_path(PREFIX, ".csv")
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(list(fields))
        for rec in records:
            w.writerow(["" if v is None else v for v in rec])

    years_str = f"{min(fetched)}-{max(fetched)}"
    return common.result_record(
        source="noaa_hdd_states_hist",
        status="ok",
        files=[raw_path.name, csv_path.name],
        note=f"历史 HDD {years_str}（{len(fetched)} 个年份文件）已落盘并解析 {len(records)} 行；-999 等缺失标记置空(NaN)。",
    )


if __name__ == "__main__":
    import json

    rec = collect()
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    # 落盘后刷新 manifest 与缺失率统计表
    import rebuild_report

    rebuild_report.rebuild()
