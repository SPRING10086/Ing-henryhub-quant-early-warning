# -*- coding: utf-8 -*-
"""
NOAA CPC 人口加权采暖度日（HDD）文本数据采集。

- 无 key，文本直下；
- 原始文本带下载时间戳落盘（保留原始文本，绝不覆盖）；
- 解析：缺失标记 -999（及 9999/./M/NA/空）统一识别为 NaN（落 CSV 时置空）；
- 兼容两种格式：
  1) 现行宽表（管道符分隔，首列 Region，其后为日期列 YYYYMMDD）——转置为长表；
  2) 传统长表（空格分隔，Year Month Day + 各州值，-999 缺测）。

URL 可用环境变量 NOAA_HDD_URL 覆盖，便于溯源到历史年度子目录。
"""
from __future__ import annotations

import csv
import os
import re

import common

# 现行人口加权 HDD（按州 + CONUS），CPC 已迁移为管道符宽表
DEFAULT_URL = (
    "https://ftp.cpc.ncep.noaa.gov/htdocs/degree_days/weighted/"
    "daily_data/latest/StatesCONUS.Heating.txt"
)
PREFIX = "noaa_hdd_states"

# 缺失标记集合（-999 为主要缺失符号，兼容其它常见占位）
MISSING = {"-999", "9999", "-999.0", ".", "M", "NA", "N/A", "", "nan", "NaN", "-9999"}


def _to_float(token: str):
    token = token.strip()
    if token in MISSING:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def _parse_wide(text: str):
    """解析宽表：'Region|20260101|20260102|...' 行按区域转置为长表记录。

    注意：多年度拼接文本会含多条 Region| 头，必须把每一行数据与其“当前”头绑定，
    避免用最后一条头的日期覆盖所有年份数据。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_dates = None
    records = []
    for ln in lines:
        if ln.startswith("Region|"):
            header_dates = ln.split("|")[1:]
            continue
        parts = ln.split("|")
        if header_dates and len(parts) == len(header_dates) + 1:
            region = parts[0].strip()
            for date, val in zip(header_dates, parts[1:]):
                records.append((date, region, _to_float(val)))
    if not records:
        return None
    return ("date", "region", "hdd"), records


def _parse_long(text: str):
    """
    解析传统长表：空格分隔，前 3 列为 Year Month Day（或首列 YYYYMMDD），
    其后为州值，-999 缺测。列名按州缩写或 col_N 命名。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    data_lines = []
    for ln in lines:
        tokens = ln.split()
        # 数据行：首 token 为纯数字日期
        if tokens and (tokens[0].isdigit()):
            data_lines.append(tokens)
    if not data_lines:
        return None

    first = data_lines[0]
    # 判断日期列：YYYYMMDD 单列，或 Year/Month/Day 三列
    if len(first[0]) == 8 and first[0].isdigit():
        date_span = 1
        nvals = len(first) - 1
    else:
        date_span = 3
        nvals = len(first) - 3
    if nvals <= 0:
        return None

    fields = ["date"] + [f"col_{i}" for i in range(nvals)]
    records = []
    for tokens in data_lines:
        if date_span == 1:
            date = tokens[0]
            vals = tokens[1:]
        else:
            y, m, d = tokens[0], tokens[1].zfill(2), tokens[2].zfill(2)
            date = f"{y}{m}{d}"
            vals = tokens[3:]
        if len(vals) < nvals:
            continue
        records.append((date,) + tuple(_to_float(v) for v in vals[:nvals]))
    return tuple(fields), records


def parse_noaa(text: str):
    """返回 (fields, records) 长表；records 中每行首列为 date。"""
    if "Region|" in text:
        return _parse_wide(text)
    return _parse_long(text)


def collect() -> dict:
    url = (os.environ.get("NOAA_HDD_URL") or DEFAULT_URL).strip()
    try:
        resp = common.http_get(url, interval=1.2, timeout=90)
        text = resp.content.decode("utf-8", errors="replace")
        if "Heating Degree Day" not in text and "Region" not in text:
            raise ValueError("NOAA 响应不是预期的 HDD 文本（缺少标题/Region 头）")

        raw_path = common.save_text(PREFIX, ".txt", text)

        parsed = parse_noaa(text)
        csv_path = None
        if parsed is None:
            note = "原始文本已落盘；解析失败（未识别格式），缺失率统计按原始文本计。"
        else:
            fields, records = parsed
            csv_path = common.unique_path(PREFIX, ".csv")
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(list(fields))
                for rec in records:
                    w.writerow(["" if v is None else v for v in rec])
            common.log.info("解析落盘: %s (%d 行)", csv_path.name, len(records))
            note = f"无 key 源，已落盘；-999 等缺失标记已置空（NaN）。解析行数 {len(records)}。"

        return common.result_record(
            source="noaa_hdd_states",
            status="ok",
            files=[raw_path.name] + ([csv_path.name] if csv_path else []),
            note=note,
        )
    except Exception as exc:  # noqa: BLE001
        common.log.exception("NOAA 采集失败")
        return common.result_record(
            source="noaa_hdd_states", status="error", files=[], note=f"采集失败: {exc}"
        )


if __name__ == "__main__":
    print(collect())
