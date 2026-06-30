"""logging 機制：設定載入、profile→level 細節程度、ops/audit 分流、request_id、隱私守線。

測試以 conftest 的 autouse `_isolate_logging` 把 log 導向暫存目錄並逐 case 重置 logger。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from govcheck.logging_setup import (
    audit,
    get_logger,
    load_log_config,
    new_request_id,
    set_request_id,
    setup_logging,
)


def _ops_log_text() -> str:
    p = Path(load_log_config()["dir"]) / "govcheck.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _audit_lines() -> list[str]:
    p = Path(load_log_config()["dir"]) / "audit.log"
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── 設定載入：profile → level ──────────────────────────────────────────────

def test_default_profile_is_prod_info():
    cfg = load_log_config()
    assert cfg["profile"] == "prod"
    assert cfg["level"] == "INFO"


def test_profile_dev_and_quiet(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "dev")
    assert load_log_config()["level"] == "DEBUG"
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "quiet")
    assert load_log_config()["level"] == "WARNING"


def test_explicit_level_overrides_profile(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "prod")
    monkeypatch.setenv("GOVCHECK_LOG_LEVEL", "DEBUG")  # 逃生門：細粒度覆蓋 profile
    assert load_log_config()["level"] == "DEBUG"


def test_invalid_profile_and_level_fall_back(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "bogus")
    cfg = load_log_config()
    assert cfg["profile"] == "prod" and cfg["level"] == "INFO"
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "dev")
    monkeypatch.setenv("GOVCHECK_LOG_LEVEL", "not-a-level")  # 無效 level → 退回 profile 推導
    assert load_log_config()["level"] == "DEBUG"


def test_rotation_env_override_and_fallback(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_MAX_BYTES", "123")
    assert load_log_config()["max_bytes"] == 123
    monkeypatch.setenv("GOVCHECK_LOG_MAX_BYTES", "")  # 無效 → 退回預設
    assert load_log_config()["max_bytes"] == 5_000_000


# ── setup_logging 冪等 ─────────────────────────────────────────────────────

def test_setup_logging_idempotent():
    setup_logging()
    n_ops = len(logging.getLogger("govcheck").handlers)
    n_audit = len(logging.getLogger("govcheck.audit").handlers)
    setup_logging()
    assert len(logging.getLogger("govcheck").handlers) == n_ops
    assert len(logging.getLogger("govcheck.audit").handlers) == n_audit


# ── 細節程度（核心需求）：prod 隱藏 DEBUG，dev 顯示 ───────────────────────────

def test_prod_hides_debug_keeps_info(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "prod")
    setup_logging()
    log = get_logger("vtest")
    log.debug("flow-detail-line")
    log.info("key-summary-line")
    text = _ops_log_text()
    assert "flow-detail-line" not in text   # DEBUG 流程行在 prod 不落檔
    assert "key-summary-line" in text        # INFO 重點仍落檔


def test_dev_shows_debug(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "dev")
    setup_logging()
    get_logger("vtest").debug("flow-detail-line")
    assert "flow-detail-line" in _ops_log_text()


# ── 稽核不受 profile 影響、與 ops 分流、JSON 格式 ──────────────────────────────

def test_audit_written_regardless_of_profile(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "quiet")  # ops 只剩 WARNING
    setup_logging()
    audit("review_done", subject="系統A", filing_unit="風控部", error=0, warn=1, passed=True)
    lines = _audit_lines()
    assert len(lines) == 1  # quiet 模式稽核仍完整落檔


def test_audit_separate_from_ops(monkeypatch):
    monkeypatch.setenv("GOVCHECK_LOG_PROFILE", "dev")
    setup_logging()
    audit("review_done", subject="系統A")
    assert "review_done" not in _ops_log_text()  # propagate=False：稽核不外溢到 ops 檔
    assert any("review_done" in ln for ln in _audit_lines())


def test_audit_line_is_json_with_fields():
    setup_logging()
    audit("review_done", subject="系統A", filing_unit="風控部", error=2, warn=3, passed=False)
    rec = json.loads(_audit_lines()[-1])
    assert rec["event"] == "review_done"
    assert rec["subject"] == "系統A"
    assert rec["filing_unit"] == "風控部"
    assert rec["error"] == 2 and rec["warn"] == 3 and rec["passed"] is False
    assert "request_id" in rec and "operator" in rec


# ── request_id 串連 ────────────────────────────────────────────────────────

def test_request_id_injected_into_ops():
    setup_logging()
    set_request_id("abcd1234")
    get_logger("vtest").info("with-rid")
    assert "[abcd1234]" in _ops_log_text()


def test_request_id_shared_between_ops_and_audit():
    setup_logging()
    rid = new_request_id()
    set_request_id(rid)
    get_logger("vtest").info("op-line")
    audit("review_done", subject="系統A")
    assert f"[{rid}]" in _ops_log_text()
    assert json.loads(_audit_lines()[-1])["request_id"] == rid


# ── 隱私守線：稽核記錄不得自動夾帶機密欄位 ─────────────────────────────────────

def test_audit_record_has_no_secret_markers():
    setup_logging()
    audit("review_done", subject="系統A", filing_unit="風控部", error=0, warn=0, passed=True)
    text = "\n".join(_audit_lines())
    for marker in ("Authorization", "Bearer", "api_key", "sk-"):
        assert marker not in text
