# WebP Image Converter

A fast, safe batch image-to-WebP converter for Windows with an interactive **Terminal UI (TUI)** and a scriptable **CLI**, powered purely by Pillow (`libwebp`) — no external binaries required.

> Looking for v1.0 (Flask Web UI, `cwebp`/`ExifTool`, and Stable Diffusion metadata)? Check out the `v1.0.0` git tag.

## Features

- **Interactive Terminal UI (`webp-converter`)**: Browse folders, preview file counts and sizes, adjust settings, watch live per-file progress and space saved, or cancel mid-batch.
- **Scriptable CLI**: Pass a folder path and flags for automated/batch workflows with clean exit codes (`0` success, `1` partial failure/skip, `2` fatal error).
- **Pure Python / Pillow**: Converts JPG, JPEG, PNG, TIFF/TIF, BMP, and GIF (including **animated GIFs** to animated WebP) in-process — no `cwebp.exe` or `ExifTool` setup needed.
- **Lossy or pixel-exact lossless**: Configurable quality (`0–100`) and compression effort (`method 0–6`), or lossless encoding with exact RGB preservation under transparency.
- **Metadata preservation**: Carries over EXIF tags, ICC color profiles, XMP packets, and standard PNG/GIF/BMP text comments (`Author`, `Software`, `Comment`, `Creation Time`), and automatically applies EXIF orientation to static images.
- **Safe by default**:
  - Original files are kept unless `--delete-originals` (or the TUI toggle) is enabled.
  - Files are written atomically via temp files and originals are only removed after verified output.
  - Never overwrites silently: `photo.jpg` + `photo.png` become `photo_jpg.webp` + `photo_png.webp`, and existing `.webp` files are left alone unless `--overwrite` is set.

## Requirements

- Windows 10 / 11 (Windows Terminal recommended for the TUI)
- Python 3.9 or higher

## Installation

### Recommended: install as a global terminal command

Using `pipx` or `uv`:

```powershell
pipx install .
# or
uv tool install .
```

Or install into your Python environment from the repository root:

```powershell
pip install -e .
```

After installation, `webp-converter` can be launched from any folder in PowerShell, Command Prompt, or Windows Terminal.

### Running without installing globally

```powershell
pip install -r requirements.txt
python WebP-Image-Converter.py
```

Or double-click / run `run-webp-converter.bat`.

## Usage

### Interactive Terminal UI (TUI)

Run with no arguments (or pass `--tui`):

```powershell
webp-converter
```

Keybindings inside the TUI:
- `Ctrl+R`: Start conversion
- `Ctrl+D`: Dry run (preview output filenames without modifying files)
- `Esc`: Cancel a running batch
- `Ctrl+Q`: Quit

### Command-Line Interface (CLI)

Pass a folder path and optional flags:

```powershell
webp-converter C:\Users\Name\Pictures\Batch [OPTIONS]
```

#### Options

- `--tui`: Launch the interactive terminal UI
- `--quality, -q`: WebP quality `0–100` (default: `90`)
- `--lossless` / `--lossy`: Pixel-exact lossless encoding / force lossy
- `--method, -m`: WebP compression effort `0–6` (`0` fastest, `6` smallest; default: `4`)
- `--keep-originals`: Keep original files after conversion (default)
- `--delete-originals`: Delete original files after successful conversion
- `--overwrite`: Overwrite existing `.webp` files instead of choosing a new name
- `--recursive` / `--no-recursive`: Scan subfolders recursively (default) or top-level only
- `--workers, -w`: Number of worker threads (default: CPU count)
- `--dry-run`: Preview planned conversions without modifying any files
- `--formats`: Supported extensions (default: `.jpg .jpeg .png .tif .tiff .bmp .gif`)
- `--log-file`: Write detailed logs to a file
- `--verbose, -v`: Enable debug logging
- `--config`: Path to a JSON configuration file

#### Examples

```powershell
# Convert a folder at quality 85
webp-converter D:\Photos --quality 85

# Lossless conversion, deleting originals only after success
webp-converter D:\Screenshots --lossless --delete-originals

# Top-level folder only, dry run
webp-converter D:\Uploads --no-recursive --dry-run

# Maximum compression effort with 8 worker threads
webp-converter D:\Exports -q 80 -m 6 -w 8
```

## Configuration

Settings can be saved from the TUI (`Save Config`) or edited in `config.json` (or `%APPDATA%\webp-converter\config.json` when installed globally):

```json
{
    "webp_quality": 90,
    "lossless": false,
    "method": 4,
    "delete_originals": false,
    "overwrite": false,
    "recursive": true,
    "supported_formats": [".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif"],
    "max_workers": 8
}
```

Command-line flags always override `config.json`.

## Running the tests

```powershell
pip install -r requirements-dev.txt
python -m pytest
```
