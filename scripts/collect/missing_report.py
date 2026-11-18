# -*- coding: utf-8 -*-
"""
缺失率统计表生成器。

对每个采集源结果记录，计算：
- 字段（列名清单）
- 时间范围（最小 / 最大日期）
- 缺失行数（任一非日期列缺失即计缺失行）
- 缺失率（缺失行数 / 总行数）

规则：
- 原始文本（.txt）不是表格，不做缺失率统计，仅标注已落盘；
- 无 key 跳过的源在表中标注「待 key 补跑」，缺失统计留空，绝不编造数值。
输出 data/raw/missing_report_<ts>.csv 与 .md。
"""
from __future__ import annotations

import csv
import json

import common

# 各源用于判定“时间列”的候选名（优先精确匹配）
DATE_CANDIDATES = ("date", "period", "gasDayStart", "observation_date", "DATE")

# 各源“测量值”字段（缺失率只统计这些字段；未列出的源回退为“所有非日期列”）
VALUE_FIELDS = {
    "fred_dhhngsp": ["DHHNGSP"],
    "eia_storage_wkly": ["value"],
    "eia_drygas_prod_monthly": ["value"],
    "eia_lng_exports_monthly": ["value"],
    "noaa_hdd_states": ["hdd"],
    "noaa_hdd_states_hist": ["hdd"],
    "gie_agsi_eu": ["full"],
}


def _is_missing(v) -> bool:
    if v is None:
        return True
    s = str(v).strip()
    return s in {"", ".", "-999", "9999", "-999.0", "M", "NA", "N/A", "nan", "NaN", "None", "null"}


def _read_rows(path: common.Path):
    """按扩展名读取为 (fields, rows_of_dict)；rows 为 list[dict]。"""
    p = common.RAW_DIR / path.name
    suffix = p.suffix.lower()
    if suffix == ".csv":
        with p.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            fields = reader.fieldnames or []
            rows = [dict(r) for r in reader]
        return fields, rows
    if suffix == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = data.get("rows", [])
        fields = sorted({k for r in rows for k in r.keys()}) if rows else []
        return fields, rows
    return [], []


def _date_field(fields: list[str]):
    for cand in DATE_CANDIDATES:
        if cand in fields:
            return cand
    return fields[0] if fields else ""


def _stats(path: common.Path, source: str):
    """计算单个表格文件的缺失统计；非表格文件返回 None。"""
    fields, rows = _read_rows(path)
    if not fields or not rows:
        return None
    date_field = _date_field(fields)
    value_fields = VALUE_FIELDS.get(source)
    if value_fields is None:
        value_fields = [f for f in fields if f != date_field]
    else:
        value_fields = [f for f in value_fields if f in fields]

    def row_missing(row):
        return any(_is_missing(row.get(f)) for f in value_fields)

    missing_rows = sum(1 for r in rows if row_missing(r))
    total = len(rows)
    rate = missing_rows / total if total else 0.0

    dates = [r.get(date_field, "") for r in rows if r.get(date_field, "")]
    time_start = min(dates) if dates else ""
    time_end = max(dates) if dates else ""

    return {
        "fields": ", ".join(fields),
        "time_start": str(time_start),
        "time_end": str(time_end),
        "total_rows": total,
        "missing_rows": missing_rows,
        "missing_rate": f"{rate:.6f}",
    }


def _is_tabular(path: common.Path) -> bool:
    return path.suffix.lower() in {".csv", ".json"}


def generate(results: list[dict]) -> list[dict]:
    """results 为各采集源的 result_record 列表。返回统计行列表。"""
    table = []
    for rec in results:
        source = rec["source"]
        if rec["status"] == "ok" and rec["files"]:
            # 逐文件：仅对表格文件（csv/json）做缺失统计；原始 txt 只标注已落盘
            parsed_any = False
            for fn in rec["files"]:
                p = common.RAW_DIR / fn
                if not p.exists():
                    continue
                if _is_tabular(p):
                    st = _stats(p, source)
                    if st is None:
                        continue
                    parsed_any = True
                    table.append(
                        {
                            "source": source,
                            "file": fn,
                            "fields": st["fields"],
                            "time_start": st["time_start"],
                            "time_end": st["time_end"],
                            "total_rows": st["total_rows"],
                            "missing_rows": st["missing_rows"],
                            "missing_rate": st["missing_rate"],
                            "status": "ok",
                            "note": "",
                        }
                    )
                else:
                    table.append(
                        _pending_row(source, "原始文本已落盘（非表格，不做缺失率统计）", "ok(原始文本)")
                    )
            # ok 但没有任何可解析文件（如 GIE 已最新跳过）
            if not parsed_any and not any(r["source"] == source and r["status"] == "ok(原始文本)" for r in table):
                table.append(_pending_row(source, rec["note"], "ok(无新增)"))
        elif rec["status"] == "skipped_no_key":
            table.append(_pending_row(source, rec["note"], "待 key 补跑"))
        else:  # error
            table.append(_pending_row(source, rec["note"], "错误"))

    order = {
        "fred_dhhngsp": 0,
        "eia_storage_wkly": 1,
        "eia_drygas_prod_monthly": 2,
        "eia_lng_exports_monthly": 3,
        "noaa_hdd_states": 4,
        "gie_agsi_eu": 5,
    }
    table.sort(key=lambda r: (order.get(r["source"], 99), r["file"]))
    return table


def _pending_row(source: str, note: str, status: str) -> dict:
    return {
        "source": source,
        "file": "",
        "fields": "",
        "time_start": "",
        "time_end": "",
        "total_rows": "",
        "missing_rows": "",
        "missing_rate": "",
        "status": status,
        "note": note,
    }


HEADERS = [
    "source",
    "file",
    "fields",
    "time_start",
    "time_end",
    "total_rows",
    "missing_rows",
    "missing_rate",
    "status",
    "note",
]


def write_reports(table: list[dict]) -> tuple[common.Path, common.Path]:
    csv_path = common.unique_path("missing_report", ".csv")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADERS, extrasaction="ignore")
        w.writeheader()
        for row in table:
            w.writerow({h: row.get(h, "") for h in HEADERS})

    md_path = common.unique_path("missing_report", ".md")
    lines = [
        "# 数据采集缺失率统计表",
        "",
        f"生成时间：{common.timestamp()}",
        "",
        "| 数据源 | 文件 | 字段 | 时间范围起 | 时间范围止 | 总行数 | 缺失行数 | 缺失率 | 状态 | 备注 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in table:
        full = {h: row.get(h, "") for h in HEADERS}
        lines.append(
            "| {source} | {file} | {fields} | {time_start} | {time_end} | {total_rows} | "
            "{missing_rows} | {missing_rate} | {status} | {note} |".format(**full)
        )
    lines.append("")
    lines.append("> 红线遵循：无 key 的源标注「待 key 补跑」，缺失统计留空，绝不编造数值。")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, md_path
