# -*- coding: utf-8 -*-
"""
公共采集基建：环境变量只读加载、带退避重试的 HTTP 客户端、
请求间隔控制、时间戳落盘（绝不覆盖旧文件）、优雅降级结果记录。

红线遵循：
- API Key 仅从环境变量 / .env 只读加载（python-dotenv，override=False），禁止硬编码；
- raw 落盘文件名带下载日期时间戳，且冲突时追加序号，永不覆盖旧文件。
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# 项目根目录：scripts/collect/ -> scripts -> 根
ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
COLLECT_DIR = ROOT / "scripts" / "collect"
ENV_PATH = ROOT / ".env"

# 只读加载 .env，不覆盖真实环境变量（override=False）
load_dotenv(dotenv_path=ENV_PATH, override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("collect")


def env_key(name: str) -> str:
    """从环境变量只读取 key，去首尾空白；绝不回退到硬编码。"""
    return (os.environ.get(name) or "").strip()


def env_key_any(*names: str) -> str:
    """按顺序读取多个候选环境变量名，返回第一个非空值；均空返回 ''。"""
    for name in names:
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return ""


# ---------------------------------------------------------------------------
# 请求间隔控制（默认 1.2s；各源可覆盖）
# ---------------------------------------------------------------------------
_last_request_ts = 0.0


def throttle(interval: float = 1.2) -> None:
    """保证相邻请求之间至少间隔 `interval` 秒。"""
    global _last_request_ts
    now = time.monotonic()
    wait = _last_request_ts + interval - now
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.monotonic()


# ---------------------------------------------------------------------------
# 带退避重试的 HTTP GET
# ---------------------------------------------------------------------------
def http_get(
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    timeout: float = 30.0,
    retries: int = 5,
    base_backoff: float = 1.0,
    max_backoff: float = 60.0,
    interval: float = 1.2,
) -> requests.Response:
    """
    GET 请求 + 指数退避（含抖动）：
    - 429：优先按 Retry-After 退避，否则指数退避；
    - 5xx / 连接错误 / 超时：指数退避；
    - 每次发起请求前执行 throttle(interval) 控制频率。
    """
    headers = dict(headers or {})
    # 实测 stlouisfed.org(FRED) 对自定义/Mozilla UA 会断连或超时，
    # 而 requests 默认 UA 可稳定返回；故默认不覆盖 UA，仅允许调用方显式传入。
    # 其他源（EIA/GIE）无需 UA 亦可正常访问。

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        throttle(interval)
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        except requests.RequestException as exc:  # 连接错误 / 超时等
            last_exc = exc
            log.warning("请求异常 (第 %d/%d 次): %s", attempt, retries, exc)
            if attempt >= retries:
                raise
            time.sleep(min(base_backoff * (2 ** attempt) + random.uniform(0, 0.5), max_backoff))
            continue

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else base_backoff * (2 ** attempt)
            except (TypeError, ValueError):
                wait = base_backoff * (2 ** attempt)
            wait = min(wait + random.uniform(0, 0.5), max_backoff)
            log.warning("429 限流 (第 %d/%d 次)，退避 %.1fs", attempt, retries, wait)
            if attempt >= retries:
                resp.raise_for_status()
            time.sleep(wait)
            continue

        if resp.status_code >= 500:
            log.warning("5xx 服务端错误 (第 %d/%d 次): HTTP %s", attempt, retries, resp.status_code)
            if attempt >= retries:
                resp.raise_for_status()
            time.sleep(min(base_backoff * (2 ** attempt) + random.uniform(0, 0.5), max_backoff))
            continue

        resp.raise_for_status()
        return resp

    raise last_exc if last_exc else RuntimeError(f"请求最终失败: {url}")


# ---------------------------------------------------------------------------
# 时间戳落盘（永不覆盖）
# ---------------------------------------------------------------------------
def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def unique_path(prefix: str, ext: str) -> Path:
    """生成带下载时间戳、且与既有文件不冲突的落盘路径（追加序号，绝不覆盖）。"""
    ts = timestamp()
    path = RAW_DIR / f"{prefix}_{ts}{ext}"
    n = 1
    while path.exists():
        path = RAW_DIR / f"{prefix}_{ts}_{n}{ext}"
        n += 1
    return path


def save_bytes(prefix: str, ext: str, content: bytes) -> Path:
    path = unique_path(prefix, ext)
    path.write_bytes(content)
    log.info("落盘(二进制): %s (%d bytes)", path.name, len(content))
    return path


def save_text(prefix: str, ext: str, content: str) -> Path:
    path = unique_path(prefix, ext)
    path.write_text(content, encoding="utf-8")
    log.info("落盘(文本): %s (%d chars)", path.name, len(content))
    return path


def save_json(prefix: str, ext: str, obj) -> Path:
    return save_text(prefix, ext, json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------------
# 优雅降级：每个采集源返回统一结果记录
# ---------------------------------------------------------------------------
def result_record(source: str, status: str, files: list[str], note: str = "") -> dict:
    return {
        "source": source,
        "status": status,  # ok | skipped_no_key | error
        "files": files,
        "note": note,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
