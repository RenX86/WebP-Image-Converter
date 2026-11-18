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


def test_plan_skips_when_all_names_taken(tmp_path):
    (tmp_path / "a.webp").write_bytes(b"x")
    (tmp_path / "a_png.webp").write_bytes(b"x")
    a = str(tmp_path / "a.png")
    plan, skipped = plan_conversions([a])
    assert plan == [] and skipped == [a]


# ---------------------------------------------------------------- scanning & config

def test_scan_non_recursive(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    Image.new("RGB", (8, 8)).save(tmp_path / "top.png")
    Image.new("RGB", (8, 8)).save(sub / "nested.png")
    assert len(scan_folder(str(tmp_path), {".png"}, recursive=True)) == 2
    assert len(scan_folder(str(tmp_path), {".png"}, recursive=False)) == 1


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
        [sys.executable, script, str(tmp_path), "--delete-originals", "-w", "2"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert r.returncode == 0, r.stderr
    assert sorted(os.listdir(tmp_path)) == ["same_jpg.webp", "same_png.webp"]

    r = subprocess.run([sys.executable, script, str(tmp_path / "missing")], capture_output=True)
    assert r.returncode == 2
