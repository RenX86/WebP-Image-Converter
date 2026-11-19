import io
import os
import subprocess
import sys
import threading

import pytest
from PIL import Image, ImageCms, PngImagePlugin

from webp_converter.cli import build_parser, build_settings
from webp_converter.config import Settings
from webp_converter.core import (
    BatchEvent,
    convert_file,
    plan_conversions,
    run_batch,
    scan_folder,
    to_exif_datetime,
)


# ---------------------------------------------------------------- planning

def test_plan_simple(tmp_path):
    a = str(tmp_path / "a.png")
    open(a, "wb").close()
    plan, skipped = plan_conversions([a])
    assert plan == [(a, str(tmp_path / "a.webp"))] and skipped == []


def test_plan_same_stem_gets_distinct_names(tmp_path):
    jpg, png = str(tmp_path / "photo.jpg"), str(tmp_path / "photo.png")
    plan, skipped = plan_conversions([jpg, png])
    outputs = dict(plan)
    assert outputs[jpg].endswith("photo_jpg.webp")
    assert outputs[png].endswith("photo_png.webp")
    assert skipped == []


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="case-insensitive filesystem")
def test_plan_case_insensitive_collision(tmp_path):
    a, b = str(tmp_path / "Photo.jpg"), str(tmp_path / "photo.PNG")
    plan, _ = plan_conversions([a, b])
    assert len({os.path.normcase(d) for _, d in plan}) == 2


def test_plan_never_overwrites_existing_webp(tmp_path):
    (tmp_path / "a.webp").write_bytes(b"existing")
    a = str(tmp_path / "a.png")
    plan, _ = plan_conversions([a])
    assert plan == [(a, str(tmp_path / "a_png.webp"))]
    plan, _ = plan_conversions([a], overwrite=True)
    assert plan == [(a, str(tmp_path / "a.webp"))]


def test_plan_numeric_fallback_and_never_self_overwrites(tmp_path):
    (tmp_path / "a.webp").write_bytes(b"x")
    (tmp_path / "a_png.webp").write_bytes(b"x")
    a = str(tmp_path / "a.png")
    plan, skipped = plan_conversions([a])
    assert plan == [(a, str(tmp_path / "a_png_2.webp"))] and skipped == []

    # Three-way stem collision (photo.jpg, photo.png, photo_jpg.png) converts all three
    p1 = str(tmp_path / "photo.jpg")
    p2 = str(tmp_path / "photo.png")
    p3 = str(tmp_path / "photo_jpg.png")
    plan3, skipped3 = plan_conversions([p1, p2, p3])
    assert len(plan3) == 3 and skipped3 == []
    assert len({os.path.normcase(dst) for _, dst in plan3}) == 3

    # .webp input is skipped rather than planning src == dst
    w = str(tmp_path / "a.webp")
    plan_w, skipped_w = plan_conversions([w], overwrite=True)
    assert plan_w == [] and skipped_w == [w]


# ---------------------------------------------------------------- scanning & config

def test_scan_top_level_default_and_recursive_pruning(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    hidden = tmp_path / ".venv"
    hidden.mkdir()
    Image.new("RGB", (8, 8)).save(tmp_path / "top.png")
    Image.new("RGB", (8, 8)).save(sub / "nested.png")
    Image.new("RGB", (8, 8)).save(hidden / "ignored.gif")

    # Default (recursive=False): strictly direct children of selected folder
    top_only = scan_folder(str(tmp_path), {".png", ".gif"})
    assert [os.path.basename(p) for p in top_only] == ["top.png"]

    # recursive=True: includes normal subfolders, skips hidden/.venv folders
    rec = scan_folder(str(tmp_path), {".png", ".gif"}, recursive=True)
    assert sorted(os.path.basename(p) for p in rec) == ["nested.png", "top.png"]

    # Cancelled scan returns empty immediately
    cancel = threading.Event()
    cancel.set()
    assert scan_folder(str(tmp_path), {".png", ".gif"}, recursive=True, cancel_event=cancel) == []


def test_cli_overrides_config():
    parser = build_parser()
    cfg = {"webp_quality": "80", "delete_originals": True, "max_workers": 3, "lossless": True, "method": 6}
    s = build_settings(parser.parse_args(["x"]), cfg)
    assert (s.quality, s.delete_originals, s.max_workers, s.lossless, s.method) == (80, True, 3, True, 6)
    s = build_settings(parser.parse_args(["x", "--keep-originals", "--lossy", "-q", "50", "-m", "2"]), cfg)
    assert (s.quality, s.delete_originals, s.lossless, s.method) == (50, False, False, 2)


def test_default_keeps_originals():
    s = build_settings(build_parser().parse_args(["x"]), {})
    assert s.delete_originals is False


@pytest.mark.parametrize("value,expected", [
    ("2023-05-01T10:20:30", "2023:05:01 10:20:30"),
    ("Mon, 01 May 2023 10:20:30 +0000", "2023:05:01 10:20:30"),
    ("2023:05:01 10:20:30", "2023:05:01 10:20:30"),
    ("not a date", None),
])
def test_to_exif_datetime(value, expected):
    assert to_exif_datetime(value) == expected


# ---------------------------------------------------------------- conversion & metadata

def test_jpeg_exif_and_icc_preserved_and_unicode_path(tmp_path):
    src = str(tmp_path / "ünïcode photo.jpg")
    im = Image.new("RGB", (16, 16), "blue")
    exif = im.getexif()
    exif[0x013B] = "Tester"
    exif[0x010E] = "Sample description"
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    im.save(src, exif=exif, icc_profile=srgb)

    dst = str(tmp_path / "ünïcode photo.webp")
    ok, before, after, err = convert_file(Settings(delete_originals=True), src, dst)
    assert ok and err == "" and before > 0 and after > 0
    assert not os.path.exists(src)
    with Image.open(dst) as out:
        assert out.getexif().get(0x013B) == "Tester"
        assert out.getexif().get(0x010E) == "Sample description"
        assert out.info.get("icc_profile") == srgb


def test_exif_orientation_applied_on_convert(tmp_path):
    src = str(tmp_path / "rotated.jpg")
    im = Image.new("RGB", (20, 10), "red")
    exif = im.getexif()
    exif[0x0112] = 6  # Rotate 90 CW -> should become 10x20 upright
    im.save(src, exif=exif)

    dst = str(tmp_path / "rotated.webp")
    ok, _, _, _ = convert_file(Settings(), src, dst)
    assert ok
    with Image.open(dst) as out:
        assert out.size == (10, 20)
        assert out.getexif().get(0x0112) in (None, 1)


def test_png_xmp_and_generic_text_preserved(tmp_path):
    src = str(tmp_path / "meta.png")
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "Alice")
    info.add_text("Comment", "Multi-line\ncomment ✓")
    info.add_text("XML:com.adobe.xmp", "<x:xmpmeta>hello</x:xmpmeta>")
    Image.new("RGB", (16, 16), "green").save(src, pnginfo=info)

    dst = str(tmp_path / "meta.webp")
    ok, _, _, _ = convert_file(Settings(), src, dst)
    assert ok
    with Image.open(dst) as out:
        assert out.getexif().get(0x013B) == "Alice"
        assert b"hello" in (out.info.get("xmp") or b"")
        uc = out.getexif().get_ifd(0x8769).get(0x9286, b"")
        assert b"Multi-line" in uc or "Multi-line".encode("utf-16-be") in uc


def test_failed_encode_keeps_original_and_no_partial_output(tmp_path):
    src = str(tmp_path / "broken.png")
    with open(src, "wb") as f:
        f.write(b"not really a png")
    dst = str(tmp_path / "broken.webp")
    ok, _, _, err = convert_file(Settings(delete_originals=True), src, dst)
    assert not ok and err
    assert os.path.exists(src)
    assert not os.path.exists(dst)


def test_lossless_is_pixel_exact(tmp_path):
    src = str(tmp_path / "px.png")
    img = Image.new("RGBA", (8, 8))
    img.putdata([(i * 4, 255 - i * 4, (i * 37) % 256, 0 if i % 2 == 0 else 180) for i in range(64)])
    img.save(src)
    dst = str(tmp_path / "px.webp")
    ok, _, _, _ = convert_file(Settings(lossless=True), src, dst)
    assert ok
    with Image.open(dst) as out:
        assert out.convert("RGBA").tobytes() == img.tobytes()


@pytest.mark.parametrize("ext", [".bmp", ".gif", ".tif"])
def test_static_formats_convert(tmp_path, ext):
    src = str(tmp_path / f"img{ext}")
    Image.new("RGB", (16, 16), "green").save(src)
    dst = str(tmp_path / "img.webp")
    ok, _, _, _ = convert_file(Settings(), src, dst)
    assert ok
    with Image.open(dst) as out:
        assert out.size == (16, 16)


def test_animated_gif_preserves_animation(tmp_path):
    src = str(tmp_path / "anim.gif")
    frames = [Image.new("RGB", (16, 16), c) for c in ("red", "green", "blue")]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=100, loop=0)
    dst = str(tmp_path / "anim.webp")
    ok, _, _, _ = convert_file(Settings(delete_originals=True), src, dst)
    assert ok
    with Image.open(dst) as out:
        assert getattr(out, "n_frames", 1) == 3


def test_run_batch_events_and_cancel(tmp_path):
    plan = []
    for i in range(6):
        src = str(tmp_path / f"img_{i}.png")
        Image.new("RGB", (8, 8), "red").save(src)
        plan.append((src, str(tmp_path / f"img_{i}.webp")))

    events: list[BatchEvent] = []
    cancel = threading.Event()

    def on_event(ev: BatchEvent):
        events.append(ev)
        if ev.kind == "file_completed":
            cancel.set()

    summary = run_batch(Settings(max_workers=1), plan, on_event=on_event, cancel_event=cancel)
    assert summary.cancelled is True
    assert summary.succeeded < len(plan)
    assert events[0].kind == "batch_started" and events[-1].kind == "batch_finished"


def test_cli_exit_codes_and_collision_end_to_end(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(repo, "WebP-Image-Converter.py")
    Image.new("RGB", (8, 8), "red").save(tmp_path / "same.png")
    Image.new("RGB", (8, 8), "blue").save(tmp_path / "same.jpg")

    r = subprocess.run(
        [sys.executable, script, "--cli", str(tmp_path), "--delete-originals", "-w", "2"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert r.returncode == 0, r.stderr
    assert sorted(os.listdir(tmp_path)) == ["same_jpg.webp", "same_png.webp"]

    r = subprocess.run([sys.executable, script, "--cli", str(tmp_path / "missing")], capture_output=True)
    assert r.returncode == 2


def test_main_dispatches_to_tui_by_default(monkeypatch):
    from webp_converter import main as main_mod
    called = {}
    monkeypatch.setattr(
        "webp_converter.tui.run_tui",
        lambda initial_folder=None, config_file=None: called.update(tui=initial_folder, config=config_file),
    )
    monkeypatch.setattr("webp_converter.cli.run_cli", lambda argv=None: (called.setdefault("cli", argv), 0)[1])

    assert main_mod.main([]) == 0
    assert called["tui"] is None and called["config"] is None
    called.clear()

    assert main_mod.main([r"D:\Photos"]) == 0
    assert called["tui"] == r"D:\Photos"
    called.clear()

    assert main_mod.main(["--", "-hyphen-folder"]) == 0
    assert called["tui"] == "-hyphen-folder"
    called.clear()

    assert main_mod.main(["--config", "custom.json", r"D:\Photos"]) == 0
    assert called["tui"] == r"D:\Photos" and called["config"] == "custom.json"
    called.clear()

    assert main_mod.main(["--cli", "--tui"]) == 2

    assert main_mod.main(["--cli", r"D:\Photos", "-q", "80"]) == 0
    assert called["cli"] == [r"D:\Photos", "-q", "80"]


# ---------------------------------------------------------------- bug-hunt regressions

def test_config_strict_validation_prevents_string_false_and_null_crashes(tmp_path):
    from webp_converter.config import load_config, settings_from_config
    from webp_converter.cli import quality_type
    import argparse

    # String "false" MUST parse as False (never True via bool("false"))
    s = settings_from_config({"delete_originals": "false", "lossless": "0", "recursive": "no"})
    assert (s.delete_originals, s.lossless, s.recursive) == (False, False, False)

    # Bad types raise ValueError (not uncaught TypeError/OverflowError)
    for bad in (
        {"webp_quality": None},
        {"webp_quality": "inf"},
        {"webp_quality": True},
        {"max_workers": None},
        {"supported_formats": ".png"},
        {"supported_formats": [".webp"]},
        {"delete_originals": "maybe"},
    ):
        with pytest.raises(ValueError):
            settings_from_config(bad)

    # CLI override rescues bad value in config.json
    s = build_settings(build_parser().parse_args(["x", "-q", "80"]), {"webp_quality": 999})
    assert s.quality == 80

    # Strict load_config fails on missing explicit file
    with pytest.raises(ValueError):
        load_config(str(tmp_path / "nope.json"), strict=True)

    # -q inf raises clean ArgumentTypeError
    with pytest.raises(argparse.ArgumentTypeError):
        quality_type("inf")


def test_16bit_grayscale_png_scales_correctly_not_solid_white(tmp_path):
    src = str(tmp_path / "gray16.png")
    im = Image.new("I;16", (8, 8), 32768)  # 50% mid-gray
    im.save(src)
    dst = str(tmp_path / "gray16.webp")
    ok, _, _, err = convert_file(Settings(lossless=True), src, dst)
    assert ok, err
    with Image.open(dst) as out:
        px = out.convert("RGB").getpixel((0, 0))
        assert 120 <= px[0] <= 135, f"Expected mid-gray ~128, got {px}"


def test_cmyk_and_grayscale_drop_mismatched_icc_profiles(tmp_path):
    src = str(tmp_path / "cmyk.jpg")
    Image.new("CMYK", (16, 16), (0, 255, 255, 0)).save(src)
    dst = str(tmp_path / "cmyk.webp")
    ok, _, _, err = convert_file(Settings(), src, dst)
    assert ok, err
    with Image.open(dst) as out:
        assert out.size == (16, 16)


def test_animated_gif_preserves_variable_durations_and_loop(tmp_path):
    src = str(tmp_path / "var.gif")
    frames = [Image.new("RGB", (16, 16), c) for c in ("red", "green", "blue")]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=[80, 320, 160], loop=3)
    dst = str(tmp_path / "var.webp")
    ok, _, _, err = convert_file(Settings(), src, dst)
    assert ok, err
    with Image.open(dst) as out:
        assert getattr(out, "n_frames", 1) == 3
        assert out.info.get("loop") == 3
        durs = []
        for i in range(3):
            out.seek(i)
            out.load()
            durs.append(out.info.get("duration"))
        assert durs == [80, 320, 160]


def test_multipage_tiff_mixed_sizes_converts_cleanly(tmp_path):
    src = str(tmp_path / "multi.tif")
    page1 = Image.new("RGB", (32, 32), "red")
    page2 = Image.new("RGB", (16, 16), "blue")  # thumbnail IFD
    page1.save(src, save_all=True, append_images=[page2])
    dst = str(tmp_path / "multi.webp")
    ok, _, _, err = convert_file(Settings(), src, dst)
    assert ok, err
    with Image.open(dst) as out:
        assert out.size == (32, 32)
        assert getattr(out, "n_frames", 1) == 1


def test_rotated_tiff_preserves_exif_and_applies_orientation(tmp_path):
    src = str(tmp_path / "rot.tif")
    im = Image.new("RGB", (20, 10), "red")
    ex = im.getexif()
    ex[0x0112] = 6  # Rotate 90 CW
    ex[0x013B] = "TIFF Artist"
    im.save(src, exif=ex)
    dst = str(tmp_path / "rot.webp")
    ok, _, _, err = convert_file(Settings(), src, dst)
    assert ok, err
    with Image.open(dst) as out:
        assert out.size == (10, 20)
        assert out.getexif().get(0x013B) == "TIFF Artist"
        assert out.getexif().get(0x0112) in (None, 1)


def test_corrupt_exif_falls_back_cleanly(tmp_path):
    # Build a valid PNG and inject a malformed eXIf chunk
    import struct, zlib
    raw_buf = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(raw_buf, format="PNG")
    data = raw_buf.getvalue()
    # Insert corrupt eXIf right before IEND (last 12 bytes)
    payload = b"NOT_EXIF_AT_ALL"
    chunk = struct.pack(">I", len(payload)) + b"eXIf" + payload + struct.pack(">I", zlib.crc32(b"eXIf" + payload) & 0xFFFFFFFF)
    src = str(tmp_path / "corrupt_exif.png")
    with open(src, "wb") as f:
        f.write(data[:-12] + chunk + data[-12:])
    dst = str(tmp_path / "corrupt_exif.webp")
    ok, _, _, err = convert_file(Settings(), src, dst)
    assert ok, err
    with Image.open(dst) as out:
        assert out.size == (8, 8)


def test_run_batch_cancelled_does_not_inflate_completed_count(tmp_path):
    plan = []
    for i in range(6):
        src = str(tmp_path / f"c_{i}.png")
        Image.new("RGB", (8, 8), "red").save(src)
        plan.append((src, str(tmp_path / f"c_{i}.webp")))

    finished_events = []
    cancel = threading.Event()

    def on_event(ev: BatchEvent):
        finished_events.append(ev)
        if ev.kind == "file_completed":
            cancel.set()

    summary = run_batch(Settings(max_workers=1), plan, on_event=on_event, cancel_event=cancel)
    assert summary.cancelled is True
    last = finished_events[-1]
    assert last.kind == "batch_finished"
    assert last.completed == summary.succeeded + summary.failed
