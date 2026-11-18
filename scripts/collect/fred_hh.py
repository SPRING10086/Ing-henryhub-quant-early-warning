# -*- coding: utf-8 -*-
"""
FRED Henry Hub 天然气现货价格 DHHNGSP 采集。

- 无 key，CSV 直下；
- 原始 CSV 带下载时间戳落盘，不覆盖；
- "." 视为缺失值（后续解析阶段处理，本模块只保留原始文件）。
"""
from __future__ import annotations

import common

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DHHNGSP"
PREFIX = "fred_dhhngsp"


def collect() -> dict:
    try:
        resp = common.http_get(URL, interval=1.2, timeout=60)
        # FRED fredgraph.csv 首行是 "DATE,DHHNGSP"；空值用 "." 表示，原样保留
        text = resp.content.decode("utf-8", errors="replace")
        head = text[:200].lower()
        # fredgraph.csv 历史用 "DATE"，近期改用 "observation_date"，两者均接受
        if "date" not in head:
            raise ValueError("FRED 响应不是预期的 fredgraph CSV（首行缺少日期列）")
        path = common.save_text(PREFIX, ".csv", text)
        return common.result_record(
            source="fred_dhhngsp",
            status="ok",
            files=[path.name],
            note="无 key 源，已落盘。缺失值以空串表示，原样保留（未做填补）。",
        )
    except Exception as exc:  # noqa: BLE001
        common.log.exception("FRED 采集失败")
        return common.result_record(
            source="fred_dhhngsp", status="error", files=[], note=f"采集失败: {exc}"
        )


if __name__ == "__main__":
    print(collect())
