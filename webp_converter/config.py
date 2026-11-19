"""Configuration loading, saving, and strict validation."""

from dataclasses import dataclass, field
import json
import logging
import math
import os
import tempfile
from typing import Any, Dict, Optional, Set, Tuple

DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_WEBP_QUALITY = 90
DEFAULT_LOSSLESS = False
DEFAULT_METHOD = 4
DEFAULT_DELETE_ORIGINALS = False
DEFAULT_OVERWRITE = False
DEFAULT_RECURSIVE = False
DEFAULT_SUPPORTED_FORMATS = [".jpg", ".jpeg", ".jfif", ".png", ".tif", ".tiff", ".bmp", ".gif"]
DEFAULT_MAX_WORKERS = os.cpu_count() or 4


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


def resolve_config_path(config_file: Optional[str] = None) -> str:
    """
    Resolve the configuration file path.

    - Explicit path (anything other than None / default sentinel): resolved as-is or relative to CWD.
    - Default (None): always uses the per-user config directory (%APPDATA%\\webp-converter\\config.json)
      so running inside arbitrary project folders never hijacks or overwrites an unrelated ./config.json.
    """
    if config_file is None:
        return user_config_path()
    cleaned = config_file.strip()
    if not cleaned:
        raise ValueError("Configuration file path cannot be empty.")
    return cleaned if os.path.isabs(cleaned) else os.path.abspath(cleaned)


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


def load_config(config_file: Optional[str] = None, *, strict: bool = False) -> Dict[str, Any]:
    """
    Load configuration from JSON.

    When strict=True (e.g. an explicit --config path), missing or invalid files raise ValueError
    instead of silently falling back to defaults.
    """
    config_path = resolve_config_path(config_file)
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                msg = f"Configuration in {config_path} must be a JSON object"
                logging.error(msg)
                if strict:
                    raise ValueError(msg)
                return {}
            logging.info(f"Configuration loaded from {config_path}")
            return data
        except (json.JSONDecodeError, OSError) as e:
            msg = f"Error reading config file {config_path}: {e}"
            logging.error(msg)
            if strict:
                raise ValueError(msg) from e
            return {}

    if os.path.exists(config_path):
        msg = f"Config path is not a file: {config_path}"
        logging.error(msg)
        if strict:
            raise ValueError(msg)
        return {}

    if config_file is not None:
        msg = f"Config file not found: {config_path}"
        if strict:
            raise ValueError(msg)
        logging.warning(f"{msg} (using defaults)")
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


def save_config(settings: Settings, config_file: Optional[str] = None) -> Optional[str]:
    """Atomically persist Settings to config.json, preserving any unrelated keys."""
    try:
        config_path = resolve_config_path(config_file)
    except ValueError as e:
        logging.error(str(e))
        return None

    existing: Dict[str, Any] = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing.update({
        "webp_quality": settings.quality,
        "lossless": settings.lossless,
        "method": settings.method,
        "delete_originals": settings.delete_originals,
        "overwrite": settings.overwrite,
        "recursive": settings.recursive,
        "supported_formats": sorted(settings.supported_formats),
        "max_workers": settings.max_workers,
    })

    tmp_path: Optional[str] = None
    try:
        cfg_dir = os.path.dirname(config_path)
        os.makedirs(cfg_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".wpc_cfg_", suffix=".json", dir=cfg_dir, delete=False
        ) as tf:
            tmp_path = tf.name
            json.dump(existing, tf, indent=4)
        os.replace(tmp_path, config_path)
        tmp_path = None
        return config_path
    except OSError as e:
        logging.error(f"Could not save config file {config_path}: {e}")
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def parse_bool(value: Any, name: str) -> bool:
    """Strictly parse a boolean config value (prevents bool('false') == True data loss)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"{name} must be a boolean (true/false), got {value!r}")


def parse_int(value: Any, name: str, lo: int, hi: Optional[int] = None, *, allow_float: bool = False) -> int:
    """Strictly parse an integer within [lo, hi], rejecting booleans, None, and NaN/Inf."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if isinstance(value, int):
        num = value
    elif isinstance(value, float):
        if not allow_float or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number, got {value!r}")
        num = int(value)
    elif isinstance(value, str):
        s = value.strip()
        try:
            if allow_float:
                f = float(s)
                if not math.isfinite(f):
                    raise ValueError(f"{name} must be a finite number, got {value!r}")
                num = int(f)
            else:
                num = int(s)
        except ValueError as e:
            raise ValueError(f"{name} must be an integer, got {value!r}") from e
    else:
        raise ValueError(f"{name} must be an integer, got {value!r}")

    if num < lo or (hi is not None and num > hi):
        bounds = f"between {lo} and {hi}" if hi is not None else f">= {lo}"
        raise ValueError(f"{name} must be {bounds}, got {num}")
    return num


def normalise_formats(formats: Any) -> Set[str]:
    """Validate and normalize a collection of input extensions (excluding .webp)."""
    if isinstance(formats, (str, bytes)) or not isinstance(formats, (list, tuple, set)):
        raise ValueError(f"supported_formats must be a list of extensions, got {formats!r}")
    result: Set[str] = set()
    for raw in formats:
        if not isinstance(raw, str):
            raise ValueError(f"Format entry must be a string, got {raw!r}")
        for part in raw.replace(",", " ").split():
            ext = part.strip().lower()
            if not ext or ext == ".":
                continue
            if not ext.startswith("."):
                ext = "." + ext
            if ext == ".webp":
                raise ValueError(".webp cannot be an input format (would collide with WebP output)")
            if any(ch in ext for ch in ("/", "\\", "*", "?")):
                raise ValueError(f"Invalid format extension: {part!r}")
            result.add(ext)
    if not result:
        raise ValueError("At least one supported image format is required.")
    return result


def settings_from_config(config: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> Settings:
    """
    Build validated Settings from a config dictionary plus optional CLI overrides.

    Overrides are merged *before* validation so a CLI flag can rescue a bad value in config.json.
    """
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object.")

    merged: Dict[str, Any] = dict(config)
    for k, v in (overrides or {}).items():
        if v is not None:
            merged[k] = v

    quality = parse_int(merged.get("webp_quality", DEFAULT_WEBP_QUALITY), "webp_quality", 0, 100, allow_float=True)
    method = parse_int(merged.get("method", DEFAULT_METHOD), "method", 0, 6)
    max_workers = parse_int(merged.get("max_workers", DEFAULT_MAX_WORKERS), "max_workers", 1)
    delete_originals = parse_bool(merged.get("delete_originals", DEFAULT_DELETE_ORIGINALS), "delete_originals")
    lossless = parse_bool(merged.get("lossless", DEFAULT_LOSSLESS), "lossless")
    overwrite = parse_bool(merged.get("overwrite", DEFAULT_OVERWRITE), "overwrite")
    recursive = parse_bool(merged.get("recursive", DEFAULT_RECURSIVE), "recursive")

    raw_formats = merged.get("supported_formats", DEFAULT_SUPPORTED_FORMATS)
    formats = normalise_formats(raw_formats)

    return Settings(
        quality=quality,
        lossless=lossless,
        method=method,
        delete_originals=delete_originals,
        overwrite=overwrite,
        recursive=recursive,
        max_workers=max_workers,
        supported_formats=formats,
    )
