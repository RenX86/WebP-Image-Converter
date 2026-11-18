import os
import subprocess
import sys

import pytest
from PIL import Image, PngImagePlugin

A1111_PARAMS = (
    "masterpiece, a cat in a hat, 日本語 ✓\n"
    "Negative prompt: ugly, blurry\n"
    "Steps: 20, Sampler: Euler a, CFG scale: 7, Seed: 1234, Size: 512x512"
)
COMFY_PROMPT = '{"3": {"class_type": "KSampler", "inputs": {"seed": 1}}}'
COMFY_WORKFLOW = '{"nodes": [{"id": 3, "type": "KSampler"}],\n "links": []}'


def make_png(path, **text):
    info = PngImagePlugin.PngInfo()
    for k, v in text.items():
        info.add_text(k, v, zip=len(v) > 50)
    Image.new("RGB", (16, 16), "red").save(path, pnginfo=info)


def read_tag(tools, path, tag):
    # Go through the script's args-file helper: exiftool can't take non-ASCII paths on the Windows command line
    conv = sys.modules["webp_converter"]
    stdout, _ = conv.run_exiftool(tools["exiftool"], ["-b", f"-{tag}", path])
    return stdout


def settings(conv, tools, **kw):
    s = conv.Settings(**kw)
    s.tools = tools
    return s


# ---------------------------------------------------------------- planning

def test_plan_simple(conv, tmp_path):
    a = str(tmp_path / "a.png")
    open(a, "wb").close()
    plan, skipped = conv.plan_conversions([a])
    assert plan == [(a, str(tmp_path / "a.webp"))] and skipped == []


def test_plan_same_stem_gets_distinct_names(conv, tmp_path):
    jpg, png = str(tmp_path / "photo.jpg"), str(tmp_path / "photo.png")
    plan, skipped = conv.plan_conversions([jpg, png])
    outputs = dict(plan)
    assert outputs[jpg].endswith("photo_jpg.webp")
    assert outputs[png].endswith("photo_png.webp")
    assert skipped == []


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="case-insensitive filesystem")
def test_plan_case_insensitive_collision(conv, tmp_path):
    a, b = str(tmp_path / "Photo.jpg"), str(tmp_path / "photo.PNG")
    plan, _ = conv.plan_conversions([a, b])
    assert len({os.path.normcase(d) for _, d in plan}) == 2


def test_plan_never_overwrites_existing_webp(conv, tmp_path):
    (tmp_path / "a.webp").write_bytes(b"existing")
    a = str(tmp_path / "a.png")
    plan, _ = conv.plan_conversions([a])
    assert plan == [(a, str(tmp_path / "a_png.webp"))]
    plan, _ = conv.plan_conversions([a], overwrite=True)
    assert plan == [(a, str(tmp_path / "a.webp"))]


def test_plan_skips_when_all_names_taken(conv, tmp_path):
    (tmp_path / "a.webp").write_bytes(b"x")
    (tmp_path / "a_png.webp").write_bytes(b"x")
    a = str(tmp_path / "a.png")
    plan, skipped = conv.plan_conversions([a])
    assert plan == [] and skipped == [a]


# ---------------------------------------------------------------- config / CLI

def test_cli_overrides_config(conv):
    parser = conv.build_parser()
    cfg = {"webp_quality": "80", "delete_originals": True, "max_workers": 3, "lossless": True}
    s = conv.build_settings(parser.parse_args(["x"]), cfg)
    assert (s.quality, s.delete_originals, s.max_workers, s.lossless) == (80, True, 3, True)
    s = conv.build_settings(parser.parse_args(["x", "--keep-originals", "--lossy", "-q", "50"]), cfg)
    assert (s.quality, s.delete_originals, s.lossless) == (50, False, False)


def test_default_keeps_originals(conv):
    s = conv.build_settings(conv.build_parser().parse_args(["x"]), {})
    assert s.delete_originals is False


def test_formats_normalised(conv):
    s = conv.build_settings(conv.build_parser().parse_args(["x", "--formats", "PNG", ".Jpg"]), {})
    assert s.supported_formats == {".png", ".jpg"}


@pytest.mark.parametrize("value,expected", [
    ("2023-05-01T10:20:30", "2023:05:01 10:20:30"),
    ("Mon, 01 May 2023 10:20:30 +0000", "2023:05:01 10:20:30"),
    ("2023:05:01 10:20:30", "2023:05:01 10:20:30"),
    ("not a date", None),
])
def test_to_exif_datetime(conv, value, expected):
    assert conv.to_exif_datetime(value) == expected


# ---------------------------------------------------------------- integration (needs cwebp + exiftool)

def test_a1111_multiline_parameters_roundtrip(conv, tools, tmp_path):
    src = str(tmp_path / "gen.png")
    make_png(src, parameters=A1111_PARAMS)
    dst = str(tmp_path / "gen.webp")
    assert conv.process_image_file(settings(conv, tools), src, dst)
    assert read_tag(tools, dst, "UserComment") == A1111_PARAMS
    assert os.path.exists(src), "originals must be kept by default"


def test_comfyui_metadata_in_comfy_format(conv, tools, tmp_path):
    src = str(tmp_path / "comfy.png")
    make_png(src, prompt=COMFY_PROMPT, workflow=COMFY_WORKFLOW)
    dst = str(tmp_path / "comfy.webp")
    assert conv.process_image_file(settings(conv, tools), src, dst)
    assert read_tag(tools, dst, "Make") == "workflow:" + COMFY_WORKFLOW
    assert read_tag(tools, dst, "Model") == "prompt:" + COMFY_PROMPT


def test_jpeg_exif_copied_and_original_deleted(conv, tools, tmp_path):
    src = str(tmp_path / "ünïcode photo.jpg")
    Image.new("RGB", (16, 16), "blue").save(src)
    conv.run_exiftool(tools["exiftool"], ["-overwrite_original", "-Artist=Tester", src])
    dst = str(tmp_path / "ünïcode photo.webp")
    assert conv.process_image_file(settings(conv, tools, delete_originals=True), src, dst)
    assert read_tag(tools, dst, "Artist") == "Tester"
    assert not os.path.exists(src)


def test_failed_encode_keeps_original_and_no_partial_output(conv, tools, tmp_path):
    src = str(tmp_path / "broken.png")
    with open(src, "wb") as f:
        f.write(b"not really a png")
    dst = str(tmp_path / "broken.webp")
    assert not conv.process_image_file(settings(conv, tools, delete_originals=True), src, dst)
    assert os.path.exists(src)
    assert not os.path.exists(dst)


def test_lossless_is_pixel_exact(conv, tools, tmp_path):
    src = str(tmp_path / "px.png")
    img = Image.new("RGB", (8, 8))
    img.putdata([(i * 4, 255 - i * 4, (i * 37) % 256) for i in range(64)])
    img.save(src)
    dst = str(tmp_path / "px.webp")
    assert conv.process_image_file(settings(conv, tools, lossless=True), src, dst)
    with Image.open(dst) as out:
        assert out.convert("RGB").tobytes() == img.tobytes()


def test_cli_exit_codes_and_collision_end_to_end(tools, tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(repo, "WebP-Image-Converter.py")
    Image.new("RGB", (8, 8), "red").save(tmp_path / "same.png")
    Image.new("RGB", (8, 8), "blue").save(tmp_path / "same.jpg")

    r = subprocess.run([sys.executable, script, str(tmp_path), "--delete-originals", "-w", "2"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert sorted(os.listdir(tmp_path)) == ["same_jpg.webp", "same_png.webp"]

    r = subprocess.run([sys.executable, script, str(tmp_path / "missing")], capture_output=True)
    assert r.returncode == 2


@pytest.mark.parametrize("ext", [".bmp", ".gif", ".tif"])
def test_static_formats_convert(conv, tools, tmp_path, ext):
    src = str(tmp_path / f"img{ext}")
    Image.new("RGB", (16, 16), "green").save(src)
    dst = str(tmp_path / "img.webp")
    assert conv.process_image_file(settings(conv, tools), src, dst)
    with Image.open(dst) as out:
        assert out.size == (16, 16)


def test_animated_gif_keeps_animation_or_is_skipped(conv, tools, tmp_path):
    src = str(tmp_path / "anim.gif")
    frames = [Image.new("RGB", (16, 16), c) for c in ("red", "green", "blue")]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=100, loop=0)
    dst = str(tmp_path / "anim.webp")
    ok = conv.process_image_file(settings(conv, tools, delete_originals=True), src, dst)
    if "gif2webp" in tools:
        assert ok
        with Image.open(dst) as out:
            assert getattr(out, "n_frames", 1) == 3
    else:
        assert not ok and os.path.exists(src), "animated GIF must not be flattened and deleted"
