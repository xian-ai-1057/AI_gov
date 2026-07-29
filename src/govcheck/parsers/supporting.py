"""Security boundary around the vendored :mod:`ts_parser` source package."""

from __future__ import annotations

import functools
import hashlib
import json
import mimetypes
import os
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field

from govcheck.logging_setup import get_logger
from govcheck.models import (
    DocumentArtifact,
    DocumentParseStatus,
    SourceUnit,
    UsageSummary,
)
from govcheck.runtime_limits import MODEL_CALL_SEMAPHORE
from ts_parser.factory import ParserFactory
from ts_parser.parsers.base_parser import ParserUnavailableError, UnsupportedFileError

log = get_logger("supporting_parser")

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "parser_config.yaml"
PARSER_SOURCE_REF = "source-snapshot-2026-07-28"

_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tif",
    ".tiff",
}
_VLM_EXTENSIONS = {
    ".doc",
    ".ppt",
    ".pptx",
}
_ZIP_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
_OLE_EXTENSIONS = {".doc", ".xls", ".ppt", ".msg"}


class ParserSettings(BaseModel):
    enabled_formats: set[str] = Field(default_factory=set)
    word_strategy: Literal["native", "melody", "auto"] = "auto"
    model_host: str | None = None
    approved_model_hosts: set[str] = Field(default_factory=lambda: {"localhost", "127.0.0.1"})
    request_timeout: float = 60.0
    max_file_bytes: int = 50 * 1024 * 1024
    max_pages: int = 200
    max_zip_uncompressed_bytes: int = 200 * 1024 * 1024
    max_zip_ratio: float = 100.0
    temp_root: str | None = None
    ocr_enabled: bool = False
    vlm_enabled: bool = False
    persistent_log_enabled: bool = False

    def normalized_formats(self) -> set[str]:
        return {value if value.startswith(".") else f".{value}" for value in self.enabled_formats}

    def model_host_is_approved(self) -> bool:
        if not self.model_host:
            return False
        parsed = urlparse(self.model_host)
        return parsed.scheme in {"http", "https"} and parsed.hostname in self.approved_model_hosts

    def safe_hash(self) -> str:
        data = self.model_dump(mode="json")
        if data.get("model_host"):
            data["model_host"] = hashlib.sha256(data["model_host"].encode()).hexdigest()
        # set 欄位序列化成 list 的順序不穩定（受 PYTHONHASHSEED 影響），
        # 而 sort_keys 只排 key 不排 value；不先排序，同一份設定在不同 process
        # 會算出不同的 parser_config_hash，稽核就無法用它證明兩次審查設定相同。
        for key, value in data.items():
            if isinstance(value, list):
                data[key] = sorted(value)
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


@functools.lru_cache(maxsize=1)
def _load_yaml(path: str) -> dict:
    source = Path(path)
    if not source.exists():
        return {}
    with source.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _as_number(env_value: str | None, raw_value, cast, default):
    """env → yaml → 硬碼預設，逐層容錯（比照 llm/config.py 的 _as_number）。

    .env 慣例會留下 `GOVCHECK_XXX=` 這種空值；直接 float("")/int("") 會丟 ValueError，
    而這個呼叫點在 per-document try 之外，會讓整份審查失敗。空值/壞值一律往下一層退。
    """
    for candidate in (env_value, raw_value):
        if candidate is None or (isinstance(candidate, str) and not candidate.strip()):
            continue
        try:
            return cast(candidate)
        except (TypeError, ValueError):
            log.warning("parser setting ignored: not a %s", cast.__name__)
    return default


def _as_choice(env_value: str | None, raw_value, allowed: set[str], default: str) -> str:
    for candidate in (env_value, raw_value):
        if isinstance(candidate, str) and candidate.strip() in allowed:
            return candidate.strip()
    return default


def load_parser_settings(path: str | None = None) -> ParserSettings:
    raw = _load_yaml(str(Path(path) if path else CONFIG_PATH))
    formats_env = os.environ.get("GOVCHECK_PARSER_ENABLED_FORMATS")
    formats = (
        {part.strip().casefold() for part in formats_env.split(",") if part.strip()}
        if formats_env is not None
        else set(raw.get("enabled_formats") or [])
    )
    hosts_env = os.environ.get("GOVCHECK_PARSER_APPROVED_MODEL_HOSTS")
    hosts = (
        {part.strip() for part in hosts_env.split(",") if part.strip()}
        if hosts_env is not None
        else set(raw.get("approved_model_hosts") or ["localhost", "127.0.0.1"])
    )
    return ParserSettings(
        enabled_formats=formats,
        word_strategy=_as_choice(
            os.environ.get("GOVCHECK_PARSER_WORD_STRATEGY"),
            raw.get("word_strategy"),
            {"native", "melody", "auto"},
            "auto",
        ),
        model_host=os.environ.get("GOVCHECK_PARSER_MODEL_HOST") or raw.get("model_host"),
        approved_model_hosts=hosts,
        request_timeout=_as_number(
            os.environ.get("GOVCHECK_PARSER_TIMEOUT"), raw.get("request_timeout"), float, 60.0),
        max_file_bytes=_as_number(
            os.environ.get("GOVCHECK_PARSER_MAX_FILE_BYTES"), raw.get("max_file_bytes"),
            int, 50 * 1024 * 1024),
        max_pages=_as_number(
            os.environ.get("GOVCHECK_PARSER_MAX_PAGES"), raw.get("max_pages"), int, 200),
        max_zip_uncompressed_bytes=_as_number(
            None, raw.get("max_zip_uncompressed_bytes"), int, 200 * 1024 * 1024),
        max_zip_ratio=_as_number(None, raw.get("max_zip_ratio"), float, 100.0),
        temp_root=os.environ.get("GOVCHECK_PARSER_TEMP_ROOT") or raw.get("temp_root"),
        ocr_enabled=_as_bool(os.environ.get("GOVCHECK_PARSER_OCR_ENABLED"), raw.get("ocr_enabled", False)),
        vlm_enabled=_as_bool(os.environ.get("GOVCHECK_PARSER_VLM_ENABLED"), raw.get("vlm_enabled", False)),
        persistent_log_enabled=False,
    )


class TsParserAdapter:
    """Only application component allowed to import and execute ``ts_parser``."""

    def __init__(self, settings: ParserSettings | None = None) -> None:
        self.settings = settings or load_parser_settings()

    def parse(self, path: str | Path) -> tuple[DocumentArtifact, UsageSummary]:
        source = Path(path)
        usage = UsageSummary()
        filename_hash = hashlib.sha256(source.name.encode()).hexdigest()[:16]
        media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        config_hash = self.settings.safe_hash()
        document_id = hashlib.sha256(str(source).encode()).hexdigest()[:20]

        try:
            raw = source.read_bytes()
            document_id = hashlib.sha256(raw).hexdigest()[:20]
            ext = source.suffix.casefold()
            self._preflight(raw, ext)
            remote_parser_call = False
            if ext in _IMAGE_EXTENSIONS:
                if self.settings.ocr_enabled:
                    usage.parser_ocr_calls += 1
                else:
                    usage.parser_vlm_calls += 1
                if (
                    not (self.settings.ocr_enabled or self.settings.vlm_enabled)
                    or not self.settings.model_host_is_approved()
                ):
                    return self._artifact(
                        document_id, filename_hash, media_type, config_hash,
                        DocumentParseStatus.PARSER_UNAVAILABLE, ["vision_endpoint_unavailable"],
                    ), usage
                remote_parser_call = True
            elif ext in _VLM_EXTENSIONS or (
                ext == ".docx" and self.settings.word_strategy == "melody"
            ):
                usage.parser_vlm_calls += 1
                if not self.settings.vlm_enabled or not self.settings.model_host_is_approved():
                    return self._artifact(
                        document_id, filename_hash, media_type, config_hash,
                        DocumentParseStatus.PARSER_UNAVAILABLE, ["vlm_endpoint_unavailable"],
                    ), usage
                remote_parser_call = True

            parser = ParserFactory.get_parser(
                {"default": True},
                word_strategy=self.settings.word_strategy,
                ocr_enabled=self.settings.ocr_enabled,
                vlm_enabled=self.settings.vlm_enabled,
                model_host=self.settings.model_host,
                timeout=self.settings.request_timeout,
                max_pages=self.settings.max_pages,
            )
            opaque_filename = f"{document_id}{ext}"
            with tempfile.TemporaryDirectory(dir=self.settings.temp_root) as temp_dir:
                if remote_parser_call:
                    with MODEL_CALL_SEMAPHORE:
                        result = parser.parse(BytesIO(raw), opaque_filename, temp_dir=temp_dir)
                else:
                    result = parser.parse(BytesIO(raw), opaque_filename, temp_dir=temp_dir)
            units = self._normalize_units(document_id, ext, result)
            if not units or not any(unit.content.strip() for unit in units):
                return self._artifact(
                    document_id, filename_hash, media_type, config_hash,
                    DocumentParseStatus.UNREADABLE, ["empty_parser_output"], units,
                ), usage
            if len(units) > self.settings.max_pages:
                return self._artifact(
                    document_id, filename_hash, media_type, config_hash,
                    DocumentParseStatus.UNREADABLE, ["page_limit_exceeded"],
                ), usage
            return self._artifact(
                document_id, filename_hash, media_type, config_hash,
                DocumentParseStatus.PARSED, [], units,
            ), usage
        except _Unsupported as exc:
            log.info("supporting parse skipped document=%s reason=%s", document_id, exc.code)
            return self._artifact(
                document_id, filename_hash, media_type, config_hash,
                DocumentParseStatus.UNSUPPORTED, [exc.code],
            ), usage
        except UnsupportedFileError:
            return self._artifact(
                document_id, filename_hash, media_type, config_hash,
                DocumentParseStatus.UNSUPPORTED, ["parser_not_supported"],
            ), usage
        except ParserUnavailableError:
            return self._artifact(
                document_id, filename_hash, media_type, config_hash,
                DocumentParseStatus.PARSER_UNAVAILABLE, ["parser_dependency_unavailable"],
            ), usage
        except Exception as exc:  # noqa: BLE001 - parser failure must not abort the review
            log.warning("supporting parse failed document=%s err=%s", document_id, type(exc).__name__)
            return self._artifact(
                document_id, filename_hash, media_type, config_hash,
                DocumentParseStatus.UNREADABLE, [type(exc).__name__],
            ), usage

    def parse_many(self, paths: list[str | Path]) -> tuple[list[DocumentArtifact], UsageSummary]:
        artifacts: list[DocumentArtifact] = []
        usage = UsageSummary()
        seen: set[str] = set()
        for path in paths:
            # dedup 必須發生在解析「之前」：內容雜湊只需重讀一次檔案（多半已在
            # page cache），而重複解析一份 PDF/xlsx 是整條管線最貴的動作。
            key = _content_key(path)
            if key is not None and key in seen:
                usage.cache_hits += 1
                continue
            artifact, item_usage = self.parse(path)
            usage.add(item_usage)
            if artifact.document_id in seen:
                usage.cache_hits += 1
                continue
            seen.add(artifact.document_id)
            artifacts.append(artifact)
        return artifacts, usage

    def _preflight(self, raw: bytes, ext: str) -> None:
        if ext not in self.settings.normalized_formats():
            raise _Unsupported("format_disabled")
        if not raw:
            raise ValueError("empty file")
        if len(raw) > self.settings.max_file_bytes:
            raise _Unsupported("file_too_large")
        if ext == ".pdf" and not raw.startswith(b"%PDF"):
            raise ValueError("invalid pdf signature")
        if ext in _ZIP_EXTENSIONS:
            if not raw.startswith(b"PK"):
                raise ValueError("invalid zip signature")
            self._check_zip(raw)
        if ext in _OLE_EXTENSIONS and not raw.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise ValueError("invalid ole signature")
        signatures = {
            ".png": (b"\x89PNG",),
            ".jpg": (b"\xff\xd8\xff",),
            ".jpeg": (b"\xff\xd8\xff",),
            ".bmp": (b"BM",),
            ".tif": (b"II*\x00", b"MM\x00*"),
            ".tiff": (b"II*\x00", b"MM\x00*"),
        }
        if ext in signatures and not any(raw.startswith(sig) for sig in signatures[ext]):
            raise ValueError("invalid image signature")

    def _check_zip(self, raw: bytes) -> None:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            total = sum(info.file_size for info in archive.infolist())
            compressed = sum(max(info.compress_size, 1) for info in archive.infolist())
        if total > self.settings.max_zip_uncompressed_bytes:
            raise _Unsupported("zip_uncompressed_limit_exceeded")
        if total / max(compressed, 1) > self.settings.max_zip_ratio:
            raise _Unsupported("zip_ratio_limit_exceeded")

    @staticmethod
    def _normalize_units(document_id: str, ext: str, result: object) -> list[SourceUnit]:
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            raise ValueError("ts_parser response contract mismatch")
        units = []
        for index, row in enumerate(result["content"], start=1):
            if not isinstance(row, dict):
                continue
            locator = str(row.get("page") or index)
            locator_type = "sheet" if ext in {".xls", ".xlsx"} else "page"
            attachment = row.get("attachment_path")
            if attachment:
                locator_type = "attachment"
            units.append(SourceUnit(
                unit_id=f"{document_id}:{index}",
                content=str(row.get("content") or ""),
                locator_type=locator_type,
                locator_value=locator,
                attachment_path=str(attachment) if attachment else None,
            ))
        return units

    @staticmethod
    def _artifact(
        document_id: str,
        filename_hash: str,
        media_type: str,
        config_hash: str,
        status: DocumentParseStatus,
        warnings: list[str],
        units: list[SourceUnit] | None = None,
    ) -> DocumentArtifact:
        return DocumentArtifact(
            document_id=document_id,
            filename_hash=filename_hash,
            media_type=media_type,
            parser_source_commit=PARSER_SOURCE_REF,
            parser_config_hash=config_hash,
            status=status,
            source_units=units or [],
            warnings=warnings,
        )


def _content_key(path: str | Path) -> str | None:
    """與 TsParserAdapter.parse 的 document_id 同演算法；讀不到檔就回 None 交給 parse 處理。"""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()[:20]


class _Unsupported(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
