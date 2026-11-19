"""UI-agnostic image scanning, planning, WebP encoding, and batch execution."""

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
import io
import logging
import os
import sys
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from PIL import Image, ImageCms, ImageSequence

_EXIF_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}

from .config import Settings

EXIF_IFD_TAG = 0x8769
TAG_ORIENTATION = 0x0112
TAG_IMAGE_DESCRIPTION = 0x010E
TAG_SOFTWARE = 0x0131
TAG_ARTIST = 0x013B
TAG_DATETIME_ORIGINAL = 0x9003
TAG_USER_COMMENT = 0x9286

# Internal TIFF layout / duplicated container tags that should not leak into WebP EXIF
_TIFF_INTERNAL_TAGS = {
    254, 255, 256, 257, 258, 259, 262, 273, 277, 278, 279,
    284, 317, 322, 323, 324, 325, 338, 339, 700, 34675,
}

IGNORED_DIR_NAMES: Set[str] = {
    "$recycle.bin",
    "system volume information",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    ".git",
}

_CANCELLED = object()


@dataclass(frozen=True)
class BatchEvent:
    """Progress/lifecycle event emitted by run_batch for CLI, TUI, or future GUIs."""

    kind: str  # batch_started | file_started | file_completed | file_failed | batch_finished
    total: int = 0
    completed: int = 0
    src: str = ""
    dst: str = ""
    bytes_before: int = 0
    bytes_after: int = 0
    error: str = ""
    summary: Optional["BatchSummary"] = None


@dataclass
class BatchSummary:
    """Aggregate result of a conversion batch."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: bool = False
    bytes_before: int = 0
    bytes_after: int = 0
    errors: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def saved_bytes(self) -> int:
        return self.bytes_before - self.bytes_after


EventCallback = Callable[[BatchEvent], None]


def is_ignored_dir(name: str) -> bool:
    """Return True for hidden dot-directories and common system/dev noise folders."""
    lower = name.lower()
    return lower.startswith(".") or lower.startswith("$") or lower in IGNORED_DIR_NAMES


def normalize_folder_path(path: str) -> str:
    """Strip quotes/whitespace and normalize Windows bare drive roots ('C:' -> 'C:\\')."""
    cleaned = path.strip().strip("\"'")
    if sys.platform.startswith("win") and len(cleaned) == 2 and cleaned[1] == ":" and cleaned[0].isalpha():
        cleaned += "\\"
    return os.path.normpath(cleaned)


def check_folder(path: str) -> Tuple[Optional[str], Optional[str]]:
    """Validate a folder path (including actual directory read access) and return (path, error)."""
    if not path or not path.strip():
        return None, "No folder path provided."
    cleaned = normalize_folder_path(path)
    if not os.path.exists(cleaned):
        return None, f"Folder does not exist: {cleaned}"
    if not os.path.isdir(cleaned):
        return None, f"Path is not a directory: {cleaned}"
    # Actually open the directory: os.access(..., os.R_OK) ignores Windows NTFS ACLs
    try:
        with os.scandir(cleaned):
            pass
    except PermissionError:
        return None, f"Folder is not readable (permission denied): {cleaned}"
    except OSError as e:
        return None, f"Folder is not readable: {e}"
    return cleaned, None


def validate_folder_path(path: str) -> Optional[str]:
    """Validate the folder path and return normalized path or None (logging any error)."""
    normalized, error = check_folder(path)
    if error:
        logging.error(error)
        return None
    return normalized


def scan_folder(
    folder_path: str,
    formats: Set[str],
    recursive: bool = False,
    cancel_event: Optional[threading.Event] = None,
    skip_hidden: bool = True,
) -> List[str]:
    """
    Find readable image files matching formats inside folder_path.

    By default (recursive=False), only direct children of folder_path are returned.
    When recursive=True, hidden/system/dev directories are skipped (unless skip_hidden=False)
    and scanning stops early if cancel_event is set.
    """
    norm_formats = tuple((f if f.startswith(".") else "." + f).lower() for f in formats)
    if not norm_formats:
        return []
    image_files: List[str] = []

    def _is_candidate_file(name: str) -> bool:
        if skip_hidden and name.startswith("._"):
            return False
        if name.lower() in norm_formats:
            # Bare dotfile like ".png" has no stem
            return False
        return name.lower().endswith(norm_formats)

    if not recursive:
        with os.scandir(folder_path) as it:
            raw_entries = []
            for entry in it:
                if cancel_event and cancel_event.is_set():
                    return []
                raw_entries.append(entry)
        raw_entries.sort(key=lambda e: e.name.lower())
        for entry in raw_entries:
            if cancel_event and cancel_event.is_set():
                return []
            if not _is_candidate_file(entry.name):
                continue
            try:
                # follow_symlinks=True so Windows OneDrive Files On-Demand (reparse points) and file symlinks work
                if not entry.is_file(follow_symlinks=True):
                    continue
                image_files.append(entry.path)
            except OSError:
                logging.warning(f"Skipping inaccessible file: {entry.path}")
        return image_files

    root_norm = os.path.normcase(os.path.abspath(folder_path))
    root_error: List[OSError] = []

    def _on_walk_error(err: OSError) -> None:
        err_path = os.path.normcase(os.path.abspath(err.filename)) if err.filename else ""
        if err_path == root_norm:
            root_error.append(err)
        else:
            logging.warning(f"Skipping unreadable directory: {err.filename} ({err})")

    for root, dirs, files in os.walk(folder_path, topdown=True, followlinks=False, onerror=_on_walk_error):
        if cancel_event and cancel_event.is_set():
            return []
        if skip_hidden:
            dirs[:] = sorted((d for d in dirs if not is_ignored_dir(d)), key=str.lower)
        else:
            dirs.sort(key=str.lower)
        for file in sorted(files, key=str.lower):
            if cancel_event and cancel_event.is_set():
                return []
            if not _is_candidate_file(file):
                continue
            file_path = os.path.join(root, file)
            try:
                if not os.path.isfile(file_path):
                    continue
                image_files.append(file_path)
            except OSError:
                logging.warning(f"Skipping inaccessible file: {file_path}")

    if root_error:
        raise root_error[0]
    return image_files


def _same_path(a: str, b: str) -> bool:
    if os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b)):
        return True
    try:
        return os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b)
    except OSError:
        return False


def plan_conversions(image_files: List[str], overwrite: bool = False) -> Tuple[List[Tuple[str, str]], List[str]]:
    """
    Decide the output path for every input so no two inputs write the same file,
    no input writes over itself (src == dst), and existing WebP files are never
    clobbered unless overwrite=True.

    - "photo.jpg" alone                       -> "photo.webp"
    - "photo.jpg" + "photo.png"               -> "photo_jpg.webp" + "photo_png.webp"
    - additional collisions                   -> "photo_jpg_2.webp", ...
    """
    def key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    groups: Dict[str, List[str]] = {}
    for src in image_files:
        groups.setdefault(key(os.path.splitext(src)[0] + ".webp"), []).append(src)

    claimed: Set[str] = set()
    plan: List[Tuple[str, str]] = []
    skipped: List[str] = []
    needs_alt: List[str] = []

    for src in image_files:
        primary = os.path.splitext(src)[0] + ".webp"
        if _same_path(src, primary):
            logging.warning(f"Skipping {src}: already a .webp file")
            skipped.append(src)
            continue
        if len(groups[key(primary)]) == 1 and (overwrite or not os.path.exists(primary)):
            claimed.add(key(primary))
            plan.append((src, primary))
        else:
            needs_alt.append(src)

    for src in needs_alt:
        stem, ext = os.path.splitext(src)
        ext_tag = ext.lstrip(".").lower() or "img"
        chosen: Optional[str] = None
        for idx in range(1, 1000):
            candidate = f"{stem}_{ext_tag}.webp" if idx == 1 else f"{stem}_{ext_tag}_{idx}.webp"
            if _same_path(src, candidate):
                continue
            if key(candidate) in claimed:
                continue
            if os.path.exists(candidate) and not overwrite:
                continue
            chosen = candidate
            break
        if chosen is None:
            logging.warning(f"Skipping {src}: could not find a free output filename")
            skipped.append(src)
            continue
        claimed.add(key(chosen))
        plan.append((src, chosen))
        logging.info(f"Name conflict: {os.path.basename(src)} -> {os.path.basename(chosen)}")

    return plan, skipped


def to_exif_datetime(value: str) -> Optional[str]:
    """Convert an ISO 8601, RFC 1123, or EXIF timestamp string into 'YYYY:MM:DD HH:MM:SS'."""
    value = value.strip()
    if not value:
        return None
    iso_val = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    parsers = (
        lambda _: datetime.fromisoformat(iso_val),
        parsedate_to_datetime,
        lambda v: datetime.strptime(v, "%Y:%m:%d %H:%M:%S"),
    )
    for parse in parsers:
        try:
            return parse(value).strftime("%Y:%m:%d %H:%M:%S")
        except (ValueError, TypeError, IndexError):
            continue
    return None


def _decode_text_bytes(raw: bytes) -> Optional[str]:
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _encode_user_comment(text: str) -> bytes:
    try:
        return b"ASCII\x00\x00\x00" + text.encode("ascii")
    except UnicodeEncodeError:
        return b"UNICODE\x00" + text.encode("utf-16-be")


def _safe_getexif(im: Image.Image) -> Image.Exif:
    """Safely parse EXIF from an image, returning an empty Exif on corrupt metadata."""
    try:
        return im.getexif()
    except Exception as e:
        logging.warning(f"Ignoring corrupt EXIF metadata: {e}")
        return Image.Exif()


def _icc_matches_rgb(icc_bytes: bytes) -> bool:
    """Return True if icc_bytes is a valid RGB ICC profile safe to attach to an RGB WebP."""
    try:
        prof = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
        return getattr(prof.profile, "xcolor_space", "").strip().upper() == "RGB"
    except Exception:
        return False


def _convert_color_mode(im: Image.Image, icc_bytes: Optional[bytes]) -> Tuple[Image.Image, Optional[bytes]]:
    """
    Convert special Pillow pixel modes (16-bit grayscale, float, CMYK, grayscale) safely
    into WebP-compatible 8-bit modes without clipping or mismatched ICC profiles.
    """
    mode = im.mode

    # 16-bit integer grayscale -> linear scale / 256 instead of clamping >= 255 to solid white
    if mode.startswith("I;16") or mode == "I":
        try:
            lo, hi = im.getextrema()
        except Exception:
            lo, hi = (0, 65535)
        if mode == "I" and hi > 65535 and hi > lo:
            scale = 255.0 / (hi - lo)
            offset = -lo * scale
            converted = im.point(lambda p: p * scale + offset).convert("L")
        else:
            converted = im.point(lambda p: p * (1.0 / 256.0)).convert("L")
        return converted, None

    # 32-bit float grayscale -> linear scale [0..1] or [lo..hi] to 8-bit L instead of truncating to 0
    if mode == "F":
        try:
            lo, hi = im.getextrema()
        except Exception:
            lo, hi = (0.0, 1.0)
        if hi <= 1.0 and lo >= 0.0:
            converted = im.point(lambda p: p * 255.0).convert("L")
        elif hi > lo:
            scale = 255.0 / (hi - lo)
            offset = -lo * scale
            converted = im.point(lambda p: p * scale + offset).convert("L")
        else:
            converted = im.convert("L")
        return converted, None

    # CMYK -> color-manage to sRGB when an ICC profile exists, otherwise convert & drop CMYK profile
    if mode == "CMYK":
        srgb_profile = ImageCms.createProfile("sRGB")
        if icc_bytes:
            try:
                src_prof = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
                converted = ImageCms.profileToProfile(im, src_prof, srgb_profile, outputMode="RGB")
                return converted, ImageCms.ImageCmsProfile(srgb_profile).tobytes()
            except Exception as e:
                logging.warning(f"CMYK ICC conversion failed, falling back to basic RGB: {e}")
        return im.convert("RGB"), None

    # Grayscale / 1-bit -> drop non-RGB (1-channel GRAY) ICC profiles so WebP stays valid
    if mode in ("1", "L", "LA"):
        if icc_bytes and not _icc_matches_rgb(icc_bytes):
            return im, None
        return im, icc_bytes

    if icc_bytes and not _icc_matches_rgb(icc_bytes):
        return im, None
    return im, icc_bytes


def extract_save_metadata(
    im: Image.Image,
    *,
    reset_orientation: bool = False,
    strip_exif_header: bool = False,
    icc_override: Optional[bytes] = None,
    use_icc_override: bool = False,
) -> Dict[str, Any]:
    """
    Collect Pillow save kwargs (icc_profile, xmp, exif) from an open image.

    Must be called on the original image (before exif_transpose drops TIFF tag_v2).
    """
    kwargs: Dict[str, Any] = {}

    icc = icc_override if use_icc_override else im.info.get("icc_profile")
    if isinstance(icc, (bytes, bytearray)) and len(icc) > 0:
        kwargs["icc_profile"] = bytes(icc)

    xmp = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")
    if isinstance(xmp, bytearray) and len(xmp) > 0:
        kwargs["xmp"] = bytes(xmp)
    elif isinstance(xmp, (bytes, str)) and len(xmp) > 0:
        kwargs["xmp"] = xmp

    exif = _safe_getexif(im)
    if im.format == "TIFF":
        for tag in list(exif.keys()):
            if tag in _TIFF_INTERNAL_TAGS:
                del exif[tag]

    if reset_orientation and TAG_ORIENTATION in exif:
        del exif[TAG_ORIENTATION]

    exif_ifd = exif.get_ifd(EXIF_IFD_TAG)
    added_text_tag = False

    for key, val in im.info.items():
        if key in ("exif", "icc_profile", "xmp", "XML:com.adobe.xmp"):
            continue
        if isinstance(val, (bytes, bytearray)):
            val = _decode_text_bytes(bytes(val))
        if not isinstance(val, str) or not val.strip():
            continue

        k = str(key).lower().replace("_", " ")
        if k in ("comment", "description") and TAG_USER_COMMENT not in exif_ifd:
            exif_ifd[TAG_USER_COMMENT] = _encode_user_comment(val)
            added_text_tag = True
        elif k in ("author", "artist") and TAG_ARTIST not in exif:
            exif[TAG_ARTIST] = val
            added_text_tag = True
        elif k == "software" and TAG_SOFTWARE not in exif:
            exif[TAG_SOFTWARE] = val
            added_text_tag = True
        elif k in ("title", "subject") and TAG_IMAGE_DESCRIPTION not in exif:
            exif[TAG_IMAGE_DESCRIPTION] = val
            added_text_tag = True
        elif k in ("creation time", "date", "datetime", "timestamp") and TAG_DATETIME_ORIGINAL not in exif_ifd:
            exif_dt = to_exif_datetime(val)
            if exif_dt:
                exif_ifd[TAG_DATETIME_ORIGINAL] = exif_dt
                added_text_tag = True

    raw_exif = im.info.get("exif")
    if (
        isinstance(raw_exif, (bytes, bytearray))
        and len(raw_exif) > 0
        and not reset_orientation
        and not added_text_tag
        and im.format != "TIFF"
    ):
        exif_bytes = bytes(raw_exif)
        if strip_exif_header and exif_bytes.startswith(b"Exif\x00\x00"):
            exif_bytes = exif_bytes[6:]
        kwargs["exif"] = exif_bytes
    elif len(exif) > 0 or len(exif_ifd) > 0:
        try:
            exif_bytes = exif.tobytes()
            if strip_exif_header and exif_bytes.startswith(b"Exif\x00\x00"):
                exif_bytes = exif_bytes[6:]
            kwargs["exif"] = exif_bytes
        except Exception as e:
            logging.warning(f"Skipping unserializable EXIF metadata: {e}")

    return kwargs


def _sanitize_background(im: Image.Image) -> Tuple[int, int, int, int]:
    """Return a valid WebP RGBA background tuple (0-255) for animated GIF/APNG."""
    bg = im.info.get("background")
    if isinstance(bg, int):
        palette = im.getpalette()
        if palette and 0 <= bg and (bg + 1) * 3 <= len(palette):
            r, g, b = palette[bg * 3 : (bg + 1) * 3]
            return (int(r), int(g), int(b), 255)
        return (0, 0, 0, 0)
    if isinstance(bg, (tuple, list)):
        if len(bg) == 3:
            vals = [max(0, min(255, int(v >> 8 if v > 255 else v))) for v in bg]
            return (vals[0], vals[1], vals[2], 255)
        if len(bg) == 4:
            vals = [max(0, min(255, int(v >> 8 if v > 255 else v))) for v in bg]
            return (vals[0], vals[1], vals[2], vals[3])
    return (0, 0, 0, 0)


def _extract_animation_options(im: Image.Image) -> Dict[str, Any]:
    """Extract per-frame durations, loop count, and safe background for animated GIF/APNG."""
    default_dur = im.info.get("duration", 100)
    if not isinstance(default_dur, (int, float)) or default_dur < 0:
        default_dur = 100
    durations: List[int] = []
    for frame in ImageSequence.Iterator(im):
        d = frame.info.get("duration", default_dur)
        durations.append(int(d) if isinstance(d, (int, float)) and d >= 0 else int(default_dur))
    im.seek(0)

    loop = im.info.get("loop", 0)
    if not isinstance(loop, int) or loop < 0:
        loop = 0

    return {
        "save_all": True,
        "duration": durations,
        "loop": loop,
        "background": _sanitize_background(im),
    }


def convert_file(settings: Settings, src: str, dst: str) -> Tuple[bool, int, int, str]:
    """
    Convert a single image to WebP using Pillow.

    Returns (success, bytes_before, bytes_after, error_message).
    Writes to a temp file in the destination folder and replaces dst atomically
    so a failure never leaves a partial .webp file behind.
    """
    if _same_path(src, dst):
        return False, 0, 0, "Source and destination paths are identical"

    try:
        bytes_before = os.path.getsize(src)
    except OSError as e:
        return False, 0, 0, str(e)

    dst_dir = os.path.dirname(os.path.abspath(dst))
    tmp_path: Optional[str] = None
    try:
        with Image.open(src) as im:
            # Multi-page TIFFs are documents/thumbnails, not animations
            animated = (
                bool(getattr(im, "is_animated", False))
                and getattr(im, "n_frames", 1) > 1
                and im.format != "TIFF"
            )

            orient = 1
            if not animated:
                orient = _safe_getexif(im).get(TAG_ORIENTATION, 1)
            needs_transpose = not animated and orient in (2, 3, 4, 5, 6, 7, 8)

            raw_icc = im.info.get("icc_profile")
            icc_bytes = bytes(raw_icc) if isinstance(raw_icc, (bytes, bytearray)) and len(raw_icc) > 0 else None

            prepared: Image.Image = im
            owned_images: List[Image.Image] = []
            try:
                if animated:
                    anim_kwargs = _extract_animation_options(im)
                    if icc_bytes and not _icc_matches_rgb(icc_bytes):
                        icc_bytes = None
                    meta = extract_save_metadata(
                        im,
                        reset_orientation=False,
                        strip_exif_header=True,
                        icc_override=icc_bytes,
                        use_icc_override=True,
                    )
                else:
                    anim_kwargs = {}
                    converted, safe_icc = _convert_color_mode(im, icc_bytes)
                    if converted is not im:
                        owned_images.append(converted)
                    meta = extract_save_metadata(
                        im,
                        reset_orientation=needs_transpose,
                        strip_exif_header=False,
                        icc_override=safe_icc,
                        use_icc_override=True,
                    )
                    if needs_transpose:
                        try:
                            transposed = converted.transpose(_EXIF_TRANSPOSE[orient])
                            owned_images.append(transposed)
                            prepared = transposed
                        except Exception as e:
                            logging.warning(f"Skipping EXIF transpose on {src}: {e}")
                            prepared = converted
                    else:
                        prepared = converted

                base_kwargs: Dict[str, Any] = {
                    "format": "WEBP",
                    "lossless": settings.lossless,
                    "quality": settings.quality,
                    "method": settings.method,
                    **anim_kwargs,
                }
                if settings.lossless and not animated:
                    base_kwargs["exact"] = True

                with tempfile.NamedTemporaryFile(
                    prefix=".wpc_", suffix=".webp", dir=dst_dir, delete=False
                ) as tf:
                    tmp_path = tf.name

                try:
                    prepared.save(tmp_path, **base_kwargs, **meta)
                except Exception as meta_err:
                    if meta:
                        logging.warning(f"Retrying {src} without metadata after encoder error: {meta_err}")
                        prepared.save(tmp_path, **base_kwargs)
                    else:
                        raise
            finally:
                for obj in owned_images:
                    try:
                        obj.close()
                    except Exception:
                        pass

        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise OSError("Encoder produced an empty output file")

        os.replace(tmp_path, dst)
        tmp_path = None
        bytes_after = os.path.getsize(dst)

        if settings.delete_originals:
            if _same_path(src, dst):
                return False, bytes_before, bytes_after, "Refused to delete original because source equals destination"
            try:
                os.remove(src)
                logging.info(f"Deleted original file: {src}")
            except OSError as del_err:
                msg = f"Converted to {os.path.basename(dst)}, but failed to delete original: {del_err}"
                logging.error(msg)
                return False, bytes_before, bytes_after, msg

        logging.info(f"Successfully processed: {src} -> {os.path.basename(dst)}")
        return True, bytes_before, bytes_after, ""

    except Exception as e:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        msg = str(e)
        logging.error(f"Failed to convert {src}: {msg}")
        return False, bytes_before, 0, msg


def run_batch(
    settings: Settings,
    plan: List[Tuple[str, str]],
    skipped: Optional[List[str]] = None,
    on_event: Optional[EventCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> BatchSummary:
    """Execute a conversion plan with a thread pool, emitting synchronized BatchEvents."""
    workers = max(1, int(settings.max_workers))
    summary = BatchSummary(total=len(plan), skipped=len(skipped or ()))
    state_lock = threading.Lock()

    def emit(event: BatchEvent) -> None:
        if on_event is None:
            return
        with state_lock:
            try:
                on_event(event)
            except Exception as cb_err:
                logging.debug(f"Ignoring error in on_event callback: {cb_err}")

    emit(BatchEvent(kind="batch_started", total=len(plan)))

    if not plan:
        emit(BatchEvent(kind="batch_finished", total=0, completed=0, summary=summary))
        return summary

    def worker(src: str, dst: str) -> Any:
        if cancel_event and cancel_event.is_set():
            return _CANCELLED
        with state_lock:
            done_so_far = summary.succeeded + summary.failed
        emit(BatchEvent(kind="file_started", total=len(plan), completed=done_so_far, src=src, dst=dst))
        return convert_file(settings, src, dst)

    completed_count = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending: Dict[Future[Any], Tuple[str, str]] = {}
        it = iter(plan)

        def submit_next() -> bool:
            if cancel_event and cancel_event.is_set():
                return False
            try:
                src, dst = next(it)
            except StopIteration:
                return False
            fut = executor.submit(worker, src, dst)
            pending[fut] = (src, dst)
            return True

        for _ in range(workers * 2):
            if not submit_next():
                break

        while pending:
            done, _ = wait(pending.keys(), return_when=FIRST_COMPLETED)
            for fut in done:
                src, dst = pending.pop(fut)
                try:
                    res = fut.result()
                except Exception as e:
                    res = (False, 0, 0, str(e))

                if res is _CANCELLED:
                    continue

                ok, b_before, b_after, err = res
                completed_count += 1
                if ok:
                    with state_lock:
                        summary.succeeded += 1
                        summary.bytes_before += b_before
                        summary.bytes_after += b_after
                    emit(BatchEvent(
                        kind="file_completed",
                        total=len(plan),
                        completed=completed_count,
                        src=src,
                        dst=dst,
                        bytes_before=b_before,
                        bytes_after=b_after,
                    ))
                else:
                    with state_lock:
                        summary.failed += 1
                        summary.errors.append((src, err))
                    emit(BatchEvent(
                        kind="file_failed",
                        total=len(plan),
                        completed=completed_count,
                        src=src,
                        dst=dst,
                        bytes_before=b_before,
                        bytes_after=b_after,
                        error=err,
                    ))
                submit_next()

        if cancel_event and cancel_event.is_set() and (summary.succeeded + summary.failed) < len(plan):
            summary.cancelled = True

    emit(BatchEvent(
        kind="batch_finished",
        total=len(plan),
        completed=completed_count,
        summary=summary,
    ))
    return summary
