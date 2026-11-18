"""UI-agnostic image scanning, planning, WebP encoding, and batch execution."""

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
import logging
import os
import tempfile
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from PIL import Image, ImageOps

from .config import Settings

EXIF_IFD_TAG = 0x8769
TAG_ORIENTATION = 0x0112
TAG_IMAGE_DESCRIPTION = 0x010E
TAG_SOFTWARE = 0x0131
TAG_ARTIST = 0x013B
TAG_DATETIME_ORIGINAL = 0x9003
TAG_USER_COMMENT = 0x9286


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


def check_folder(path: str) -> Tuple[Optional[str], Optional[str]]:
    """Validate a folder path and return (normalized_path, error_message)."""
    if not path or not path.strip():
        return None, "No folder path provided."
    cleaned = os.path.normpath(path.strip().strip("\"'"))
    if not os.path.exists(cleaned):
        return None, f"Folder does not exist: {cleaned}"
    if not os.path.isdir(cleaned):
        return None, f"Path is not a directory: {cleaned}"
    if not os.access(cleaned, os.R_OK):
        return None, f"Folder is not readable: {cleaned}"
    return cleaned, None


def validate_folder_path(path: str) -> Optional[str]:
    """Validate the folder path and return normalized path or None (logging any error)."""
    normalized, error = check_folder(path)
    if error:
        logging.error(error)
        return None
    return normalized


def scan_folder(folder_path: str, formats: Set[str], recursive: bool = True) -> List[str]:
    """Find readable image files matching formats inside folder_path."""
    norm_formats = tuple(f.lower() for f in formats)
    image_files: List[str] = []

    if recursive:
        walker: Iterable[Tuple[str, List[str], List[str]]] = os.walk(folder_path)
    else:
        entries = sorted(os.listdir(folder_path))
        walker = [(folder_path, [], [e for e in entries if os.path.isfile(os.path.join(folder_path, e))])]

    for root, _, files in walker:
        for file in sorted(files):
            if file.lower().endswith(norm_formats):
                file_path = os.path.join(root, file)
                if os.path.isfile(file_path) and os.access(file_path, os.R_OK):
                    image_files.append(file_path)
                else:
                    logging.warning(f"Skipping inaccessible file: {file_path}")
    return image_files


def plan_conversions(image_files: List[str], overwrite: bool = False) -> Tuple[List[Tuple[str, str]], List[str]]:
    """
    Decide the output path for every input so no two inputs write the same file
    and existing WebP files are never clobbered unless overwrite=True.

    - "photo.jpg" alone                    -> "photo.webp"
    - "photo.jpg" + "photo.png"            -> "photo_jpg.webp" + "photo_png.webp"
    - "photo.jpg" with existing photo.webp -> "photo_jpg.webp" (or overwrite when overwrite=True)
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
        if len(groups[key(primary)]) == 1 and (overwrite or not os.path.exists(primary)):
            claimed.add(key(primary))
            plan.append((src, primary))
        else:
            needs_alt.append(src)

    for src in needs_alt:
        stem, ext = os.path.splitext(src)
        alt = f"{stem}_{ext.lstrip('.').lower()}.webp"
        if key(alt) in claimed or (os.path.exists(alt) and not overwrite):
            logging.warning(f"Skipping {src}: output name conflict ({os.path.basename(alt)} already exists)")
            skipped.append(src)
            continue
        claimed.add(key(alt))
        plan.append((src, alt))
        logging.info(f"Name conflict: {os.path.basename(src)} -> {os.path.basename(alt)}")

    return plan, skipped


def to_exif_datetime(value: str) -> Optional[str]:
    """Convert an ISO 8601, RFC 1123, or EXIF timestamp string into 'YYYY:MM:DD HH:MM:SS'."""
    value = value.strip()
    if not value:
        return None
    parsers = (
        lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
        parsedate_to_datetime,
        lambda v: datetime.strptime(v, "%Y:%m:%d %H:%M:%S"),
    )
    for parse in parsers:
        try:
            return parse(value).strftime("%Y:%m:%d %H:%M:%S")
        except (ValueError, TypeError, IndexError):
            continue
    return None


def _encode_user_comment(text: str) -> bytes:
    try:
        return b"ASCII\x00\x00\x00" + text.encode("ascii")
    except UnicodeEncodeError:
        return b"UNICODE\x00" + text.encode("utf-16-be")


def extract_save_metadata(im: Image.Image) -> Dict[str, Any]:
    """
    Collect Pillow save kwargs (icc_profile, xmp, exif) from an open image.

    Preserves native EXIF, ICC color profiles, and XMP directly; for formats like
    PNG/BMP/GIF/TIFF without a raw EXIF blob, preserves standard text/EXIF tags.
    """
    kwargs: Dict[str, Any] = {}

    icc = im.info.get("icc_profile")
    if isinstance(icc, (bytes, bytearray)) and len(icc) > 0:
        kwargs["icc_profile"] = bytes(icc)

    xmp = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")
    if isinstance(xmp, (bytes, bytearray, str)) and len(xmp) > 0:
        kwargs["xmp"] = xmp

    raw_exif = im.info.get("exif")
    if isinstance(raw_exif, (bytes, bytearray)) and len(raw_exif) > 0:
        kwargs["exif"] = bytes(raw_exif)
        return kwargs

    exif = im.getexif()
    exif_ifd = exif.get_ifd(EXIF_IFD_TAG)

    for key, val in im.info.items():
        if isinstance(val, bytes):
            try:
                val = val.decode("utf-8")
            except UnicodeDecodeError:
                continue
        if not isinstance(val, str) or not val.strip():
            continue

        k = str(key).lower().replace("_", " ")
        if k in ("comment", "description") and TAG_USER_COMMENT not in exif_ifd:
            exif_ifd[TAG_USER_COMMENT] = _encode_user_comment(val)
        elif k in ("author", "artist") and TAG_ARTIST not in exif:
            exif[TAG_ARTIST] = val
        elif k == "software" and TAG_SOFTWARE not in exif:
            exif[TAG_SOFTWARE] = val
        elif k in ("title", "subject") and TAG_IMAGE_DESCRIPTION not in exif:
            exif[TAG_IMAGE_DESCRIPTION] = val
        elif k in ("creation time", "date", "datetime", "timestamp") and TAG_DATETIME_ORIGINAL not in exif_ifd:
            exif_dt = to_exif_datetime(val)
            if exif_dt:
                exif_ifd[TAG_DATETIME_ORIGINAL] = exif_dt

    if len(exif) > 0 or len(exif_ifd) > 0:
        kwargs["exif"] = exif

    return kwargs


def _prepare_image(im: Image.Image) -> Tuple[Image.Image, bool]:
    """
    Return (image_to_save, is_animated).

    Applies EXIF orientation to static images so pixels match display orientation
    even in viewers that ignore WebP EXIF Orientation.
    """
    animated = bool(getattr(im, "is_animated", False)) and getattr(im, "n_frames", 1) > 1
    if animated:
        return im, True
    if im.getexif().get(TAG_ORIENTATION, 1) != 1:
        return ImageOps.exif_transpose(im), False
    return im, False


def convert_file(settings: Settings, src: str, dst: str) -> Tuple[bool, int, int, str]:
    """
    Convert a single image to WebP using Pillow.

    Returns (success, bytes_before, bytes_after, error_message).
    Writes to a temp file in the destination folder and replaces dst atomically
    so a failure never leaves a partial .webp file behind.
    """
    try:
        bytes_before = os.path.getsize(src)
    except OSError as e:
        return False, 0, 0, str(e)

    dst_dir = os.path.dirname(os.path.abspath(dst))
    tmp_path: Optional[str] = None
    try:
        with Image.open(src) as im:
            prepared, animated = _prepare_image(im)
            try:
                meta = extract_save_metadata(prepared)
                save_kwargs: Dict[str, Any] = {
                    "format": "WEBP",
                    "lossless": settings.lossless,
                    "quality": settings.quality,
                    "method": settings.method,
                    **meta,
                }
                if settings.lossless and not animated:
                    save_kwargs["exact"] = True
                if animated:
                    save_kwargs["save_all"] = True

                with tempfile.NamedTemporaryFile(
                    prefix=".wpc_", suffix=".webp", dir=dst_dir, delete=False
                ) as tf:
                    tmp_path = tf.name

                prepared.save(tmp_path, **save_kwargs)
            finally:
                if prepared is not im:
                    prepared.close()

        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise OSError("Encoder produced an empty output file")

        os.replace(tmp_path, dst)
        tmp_path = None
        bytes_after = os.path.getsize(dst)

        if settings.delete_originals:
            os.remove(src)
            logging.info(f"Deleted original file: {src}")

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
    """Execute a conversion plan with a thread pool, emitting BatchEvents."""
    summary = BatchSummary(total=len(plan), skipped=len(skipped or ()))

    def emit(event: BatchEvent) -> None:
        if on_event is not None:
            on_event(event)

    emit(BatchEvent(kind="batch_started", total=len(plan)))

    if not plan:
        emit(BatchEvent(kind="batch_finished", total=0, completed=0, summary=summary))
        return summary

    def worker(src: str, dst: str) -> Tuple[bool, int, int, str]:
        if cancel_event and cancel_event.is_set():
            return False, 0, 0, "Cancelled"
        emit(BatchEvent(kind="file_started", total=len(plan), completed=summary.succeeded + summary.failed, src=src, dst=dst))
        return convert_file(settings, src, dst)

    completed_count = 0
    with ThreadPoolExecutor(max_workers=settings.max_workers) as executor:
        pending: Dict[Future[Tuple[bool, int, int, str]], Tuple[str, str]] = {}
        it = iter(plan)

        # Submit up to max_workers * 2 initially so cancellation stops queued items cleanly
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

        for _ in range(settings.max_workers * 2):
            if not submit_next():
                break

        while pending:
            done, _ = wait(pending.keys(), return_when=FIRST_COMPLETED)
            for fut in done:
                src, dst = pending.pop(fut)
                completed_count += 1
                try:
                    ok, b_before, b_after, err = fut.result()
                except Exception as e:
                    ok, b_before, b_after, err = False, 0, 0, str(e)

                if ok:
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
                    if err != "Cancelled":
                        summary.failed += 1
                        summary.errors.append((src, err))
                        emit(BatchEvent(
                            kind="file_failed",
                            total=len(plan),
                            completed=completed_count,
                            src=src,
                            dst=dst,
                            error=err,
                        ))
                submit_next()

        if cancel_event and cancel_event.is_set():
            summary.cancelled = True

    emit(BatchEvent(
        kind="batch_finished",
        total=len(plan),
        completed=completed_count,
        summary=summary,
    ))
    return summary
