"""地端 logging 機制：技術維運 log + 治理稽核 log，兩條分流。

設計沿用 llm.config 的「YAML 非機密預設 + GOVCHECK_LOG_* 環境變數覆寫」模式，純標準庫實作。

兩條 logger：
- ``govcheck``（ops，text 格式）→ logs/govcheck.log + console；level 由 profile 決定，
  這就是「細節程度」總開關（dev=DEBUG 全流程 / prod=INFO 重點 / quiet=WARNING）。
- ``govcheck.audit``（稽核，JSON-lines，propagate=False）→ logs/audit.log；level 固定 INFO、
  不隨 profile，確保每次審查的重點結果在任何模式都完整落檔（合規軌跡）。

同一次審查的 ops 與 audit 記錄以 ``request_id``（contextvars + logging.Filter）串連。

禁止記錄（隱私關鍵；地端不外送，但 log 仍會落地保存）：
  原始檔 bytes、解析後儲存格值、F03 佐證全文、LLM prompt/回應全文、api_key、Authorization header。
呼叫端只傳「識別資訊與數量」（系統名/送件單位/Finding 代碼/計數/耗時/例外型別）。
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import os
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "log_config.yaml"
# 本檔位於 src/govcheck/ → parents[2] 為 repo root（預設 logs/ 落於此）
_REPO_ROOT = Path(__file__).resolve().parents[2]

_OPS_LOGGER = "govcheck"
_AUDIT_LOGGER = "govcheck.audit"
_CONFIGURED_FLAG = "_govcheck_configured"

# profile → ops level（細節程度）。dev 記全流程、prod 只記重點、quiet 僅錯誤。
_PROFILE_LEVEL = {"dev": "DEBUG", "prod": "INFO", "quiet": "WARNING"}
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

_TRUE = {"1", "true", "yes", "on", "y", "t"}

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("govcheck_request_id", default="-")


@functools.lru_cache(maxsize=1)
def _load_yaml(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _as_bool(env_val: str | None, default) -> bool:
    if env_val is not None:
        return env_val.strip().lower() in _TRUE
    return bool(default)


def _as_number(env_val: str | None, default, cast, fallback):
    """環境變數優先、YAML 預設次之；兩者皆無效（空字串/非數字）才用 fallback。"""
    for candidate in (env_val, default):
        if candidate is None or candidate == "":
            continue
        try:
            return cast(candidate)
        except (TypeError, ValueError):
            continue
    return fallback


def _resolve_level(env_level: str | None, yaml_level, profile: str) -> str:
    """細粒度逃生門：GOVCHECK_LOG_LEVEL 或 YAML level 有效則用之，否則由 profile 推導；皆無效退 INFO。"""
    for candidate in (env_level, yaml_level):
        if candidate is None or candidate == "":
            continue
        name = str(candidate).strip().upper()
        if name in _VALID_LEVELS:
            return name
    return _PROFILE_LEVEL.get(profile, "INFO")


def load_log_config(path: str | None = None) -> dict:
    """回傳正規化後的 logging 設定 dict。

    環境變數覆寫鍵：GOVCHECK_LOG_PROFILE / _LEVEL / _DIR / _CONSOLE / _MAX_BYTES /
    _BACKUP_COUNT，及 GOVCHECK_OPERATOR（稽核「誰」，目前無登入機制故 best-effort）。
    """
    raw = _load_yaml(str(Path(path) if path else CONFIG_PATH))

    profile = (os.environ.get("GOVCHECK_LOG_PROFILE") or raw.get("profile") or "prod").strip().lower()
    if profile not in _PROFILE_LEVEL:
        profile = "prod"

    level = _resolve_level(os.environ.get("GOVCHECK_LOG_LEVEL"), raw.get("level"), profile)

    dir_val = os.environ.get("GOVCHECK_LOG_DIR") or raw.get("dir") or str(_REPO_ROOT / "logs")

    return {
        "profile": profile,
        "level": level,
        "dir": dir_val,
        "console": _as_bool(os.environ.get("GOVCHECK_LOG_CONSOLE"), raw.get("console", True)),
        "max_bytes": _as_number(os.environ.get("GOVCHECK_LOG_MAX_BYTES"), raw.get("max_bytes"), int, 5_000_000),
        "backup_count": _as_number(os.environ.get("GOVCHECK_LOG_BACKUP_COUNT"), raw.get("backup_count"), int, 5),
        "operator": os.environ.get("GOVCHECK_OPERATOR") or raw.get("operator") or "-",
    }


class RequestIdFilter(logging.Filter):
    """把當前 contextvar 的 request_id 注入每筆 record（缺則 "-"），供 formatter 串連 ops/audit。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


def new_request_id() -> str:
    """產生短 request_id（8 hex）。"""
    return uuid.uuid4().hex[:8]


def set_request_id(rid: str) -> None:
    _request_id.set(rid)


def get_request_id() -> str:
    return _request_id.get()


def _rotating_handler(path: Path, cfg: dict, fmt: str) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path, maxBytes=cfg["max_bytes"], backupCount=cfg["backup_count"], encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(RequestIdFilter())
    return handler


def _clear_handlers(logger: logging.Logger) -> None:
    """關閉並移除 logger 既有 handler（先 close 釋放檔案描述符，再 removeHandler）。"""
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def setup_logging(path: str | None = None) -> None:
    """設定 ops 與 audit 兩條 logger；冪等（Streamlit rerun / pytest 多次呼叫安全）。

    以 ops logger 的 sentinel 屬性擋重覆掛 handler。細節程度由 cfg['level']（profile 推導）決定。
    """
    ops = logging.getLogger(_OPS_LOGGER)
    if getattr(ops, _CONFIGURED_FLAG, False):
        return

    cfg = load_log_config(path)
    log_dir = Path(cfg["dir"])
    log_dir.mkdir(parents=True, exist_ok=True)

    # ── ops logger：text 格式，level 即細節程度總開關 ───────────────────────────
    # 先清掉任何殘留 handler（防呆：通過 sentinel 即代表未設定，但避免部分初始化殘留致重複輸出）。
    _clear_handlers(ops)
    ops.setLevel(cfg["level"])
    ops.propagate = False  # 不外溢到 root，避免重複輸出
    ops_fmt = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
    ops.addHandler(_rotating_handler(log_dir / "govcheck.log", cfg, ops_fmt))
    if cfg["console"]:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(ops_fmt))
        console.addFilter(RequestIdFilter())
        ops.addHandler(console)

    # ── audit logger：JSON-lines，level 固定 INFO、不隨 profile（稽核軌跡不可因降細節而消失）──
    audit_logger = logging.getLogger(_AUDIT_LOGGER)
    _clear_handlers(audit_logger)  # close 既有 handler 釋放 fd，再掛新的
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False  # 不外溢到 ops 檔（兩檔內容互不污染）
    audit_logger.addHandler(_rotating_handler(log_dir / "audit.log", cfg, "%(message)s"))

    setattr(ops, _CONFIGURED_FLAG, True)


def reset_logging() -> None:
    """清掉兩條 logger 的 handler 與冪等旗標，讓下次 setup_logging() 重新設定。

    主要供測試在每個 case 間重置（避免殘留 handler 指向上一個 tmp 目錄）。
    """
    for name in (_OPS_LOGGER, _AUDIT_LOGGER):
        _clear_handlers(logging.getLogger(name))
    ops = logging.getLogger(_OPS_LOGGER)
    if hasattr(ops, _CONFIGURED_FLAG):
        delattr(ops, _CONFIGURED_FLAG)


def get_logger(name: str) -> logging.Logger:
    """取 ops 子 logger（govcheck.<name>）。各模組以此記技術維運 log。"""
    return logging.getLogger(f"{_OPS_LOGGER}.{name}")


def audit(event: str, **fields) -> None:
    """寫一筆治理稽核記錄（一行 JSON）到 audit.log。

    只傳識別資訊與數量（如 subject / filing_unit / 計數 / 耗時），不得傳檔內容或佐證全文。
    operator / request_id 自動補上。
    """
    record = {
        "event": event,
        "request_id": get_request_id(),
        "operator": load_log_config()["operator"],
        **fields,
    }
    logging.getLogger(_AUDIT_LOGGER).info(json.dumps(record, ensure_ascii=False))
