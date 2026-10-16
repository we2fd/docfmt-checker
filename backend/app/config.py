"""应用配置：集中所有路径、限制与默认值，支持环境变量覆盖。

约定：所有可调参数以 DOCFMT_ 前缀的环境变量覆盖，便于答辩现场临时调整。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

__version__ = "1.0.0"

# backend/app/config.py -> backend/app -> backend -> 工程根
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_str(key: str, default: str) -> str:
    value = os.getenv(key)
    return value if value else default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def _env_path(key: str, default: Path) -> Path:
    raw = os.getenv(key)
    return Path(raw).expanduser().resolve() if raw else default


@dataclass
class Settings:
    app_name: str = "格致 · 学术文档格式智能检查与规范化助手"
    app_code: str = "DocFmtChecker"
    version: str = __version__
    host: str = field(default_factory=lambda: _env_str("DOCFMT_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("DOCFMT_PORT", 8300))
    api_prefix: str = "/api/v1"

    project_root: Path = field(default_factory=lambda: PROJECT_ROOT)
    frontend_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "frontend")
    data_dir: Path = field(default_factory=lambda: _env_path("DOCFMT_DATA_DIR", PROJECT_ROOT / "data"))
    rules_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "backend" / "rules")

    max_upload_mb: int = field(default_factory=lambda: _env_int("DOCFMT_MAX_UPLOAD_MB", 20))
    allowed_extensions: tuple[str, ...] = (".docx",)
    # 明确不支持、但会被很自然地误传进来的格式，用于给出「可操作」的报错而不是干巴巴的失败
    known_unsupported: tuple[str, ...] = (".doc", ".wps", ".pdf", ".odt", ".rtf", ".txt", ".md")

    cors_origins: tuple[str, ...] = ("*",)
    default_ruleset: str = "default"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def fixed_dir(self) -> Path:
        return self.data_dir / "fixed"

    @property
    def templates_dir(self) -> Path:
        """参考模板的落盘目录：模板只用于「反推规则」，不进入待检查文档列表。"""
        return self.data_dir / "templates"

    @property
    def samples_dir(self) -> Path:
        return self.project_root / "samples"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "metastore.db"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.uploads_dir, self.fixed_dir, self.templates_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
