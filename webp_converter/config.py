"""Configuration loading, saving, and validation."""

from dataclasses import dataclass, field
import json
import logging
import os
from typing import Any, Dict, Optional, Set

DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_WEBP_QUALITY = 90
DEFAULT_LOSSLESS = False
DEFAULT_METHOD = 4
DEFAULT_DELETE_ORIGINALS = False
DEFAULT_OVERWRITE = False
DEFAULT_RECURSIVE = True
DEFAULT_SUPPORTED_FORMATS = [".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif"]
DEFAULT_MAX_WORKERS = os.cpu_count() or 4

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Settings:
    """Runtime conversion settings resolved from config.json and CLI/TUI options."""

    quality: int = DEFAULT_WEBP_QUALITY
    lossless: bool = DEFAULT_LOSSLESS
    method: int = DEFAULT_METHOD
    delete_originals: bool = DEFAULT_DELETE_ORIGINALS
    overwrite: bool = DEFAULT_OVERWRITE
    recursive: bool = DEFAULT_RECURSIVE
    max_workers: int = DEFAULT_MAX_WORKERS
    supported_formats: Set[str] = field(default_factory=lambda: set(DEFAULT_SUPPORTED_FORMATS))


def user_config_path() -> str:
    """Return the per-user configuration file path on Windows (or XDG fallback)."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, "webp-converter", DEFAULT_CONFIG_FILE)
    return os.path.join(os.path.expanduser("~"), ".config", "webp-converter", DEFAULT_CONFIG_FILE)


def resolve_config_path(config_file: str = DEFAULT_CONFIG_FILE) -> str:
    """
    Resolve which config file to read.

    Explicit paths or a local config.json in the CWD or repo root take precedence;
    otherwise fall back to the per-user config directory.
    """
    if os.path.isabs(config_file):
        return config_file
    if config_file != DEFAULT_CONFIG_FILE:
        return os.path.abspath(config_file)
    if os.path.exists(config_file):
        return os.path.abspath(config_file)
    repo_cfg = os.path.join(_PROJECT_ROOT, config_file)
    if os.path.exists(repo_cfg):
        return repo_cfg
    return user_config_path()


def _default_config_dict() -> Dict[str, Any]:
    return {
        "webp_quality": DEFAULT_WEBP_QUALITY,
        "lossless": DEFAULT_LOSSLESS,
        "method": DEFAULT_METHOD,
        "delete_originals": DEFAULT_DELETE_ORIGINALS,
        "overwrite": DEFAULT_OVERWRITE,
        "recursive": DEFAULT_RECURSIVE,
        "supported_formats": DEFAULT_SUPPORTED_FORMATS,
        "max_workers": DEFAULT_MAX_WORKERS,
    }


def load_config(config_file: str = DEFAULT_CONFIG_FILE) -> Dict[str, Any]:
    """Load configuration from JSON, returning {} (and creating defaults when needed) on miss."""
    config_path = resolve_config_path(config_file)
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                logging.info(f"Configuration loaded from {config_path}")
                return data
            logging.error(f"Configuration in {config_path} must be a JSON object")
            return {}
        except (json.JSONDecodeError, OSError) as e:
            logging.error(f"Error reading config file {config_path}: {e}")
            return {}

    if config_file != DEFAULT_CONFIG_FILE:
        logging.warning(f"Config file not found: {config_path} (using defaults)")
        return {}

    defaults = _default_config_dict()
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(defaults, f, indent=4)
        logging.info(f"Default configuration file created: {config_path}")
    except OSError as e:
        logging.debug(f"Could not create default config file: {e}")
    return defaults


def save_config(settings: Settings, config_file: str = DEFAULT_CONFIG_FILE) -> Optional[str]:
    """Persist Settings to config.json and return the path written, or None on error."""
    config_path = resolve_config_path(config_file)
    payload = {
        "webp_quality": settings.quality,
        "lossless": settings.lossless,
        "method": settings.method,
        "delete_originals": settings.delete_originals,
        "overwrite": settings.overwrite,
        "recursive": settings.recursive,
        "supported_formats": sorted(settings.supported_formats),
        "max_workers": settings.max_workers,
    }
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4)
        return config_path
    except OSError as e:
        logging.error(f"Could not save config file {config_path}: {e}")
        return None


def normalise_formats(formats: Any) -> Set[str]:
    return {(str(f) if str(f).startswith(".") else f".{f}").lower() for f in formats}


def settings_from_config(config: Dict[str, Any]) -> Settings:
    """Build validated Settings from a config dictionary."""
    quality = int(float(config.get("webp_quality", DEFAULT_WEBP_QUALITY)))
    if not 0 <= quality <= 100:
        raise ValueError("quality must be between 0 and 100")

    method = int(config.get("method", DEFAULT_METHOD))
    if not 0 <= method <= 6:
        raise ValueError("method must be between 0 and 6")

    max_workers = int(config.get("max_workers", DEFAULT_MAX_WORKERS))
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")

    raw_formats = config.get("supported_formats", DEFAULT_SUPPORTED_FORMATS)
    formats = normalise_formats(raw_formats) if raw_formats else set(DEFAULT_SUPPORTED_FORMATS)

    return Settings(
        quality=quality,
        lossless=bool(config.get("lossless", DEFAULT_LOSSLESS)),
        method=method,
        delete_originals=bool(config.get("delete_originals", DEFAULT_DELETE_ORIGINALS)),
        overwrite=bool(config.get("overwrite", DEFAULT_OVERWRITE)),
        recursive=bool(config.get("recursive", DEFAULT_RECURSIVE)),
        max_workers=max_workers,
        supported_formats=formats,
    )
