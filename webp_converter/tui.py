"""Interactive Nord-themed Terminal UI (TUI) powered by Textual."""

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import string
import sys
import threading
from typing import Dict, Iterable, List, Optional, Set, Tuple

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    SelectionList,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)
from textual.widgets.selection_list import Selection

from . import __version__
from .cli import format_bytes
from .config import (
    DEFAULT_SUPPORTED_FORMATS,
    Settings,
    load_config,
    save_config,
    settings_from_config,
)
from .core import (
    BatchEvent,
    check_folder,
    is_ignored_dir,
    normalize_folder_path,
    plan_conversions,
    run_batch,
    scan_folder,
)

# Nord palette hex values for Rich Text / RichLog markup
NORD_FROST = "#88C0D0"
NORD_BLUE = "#81A1C1"
NORD_GREEN = "#A3BE8C"
NORD_YELLOW = "#EBCB8B"
NORD_RED = "#BF616A"
NORD_DIM = "#616E88"

# Preset definitions: id -> (dropdown_label, button_label, description, quality, method, lossless)
PRESETS: Dict[str, Tuple[str, str, str, Optional[int], Optional[int], Optional[bool]]] = {
    "smallest": (
        "Smallest (q=75, m=6)",
        "Smallest",
        "Smallest (q=75, m=6) — Maximum size reduction for JPG & web images.",
        75,
        6,
        False,
    ),
    "balanced": (
        "Balanced (q=82, m=4)",
        "Balanced",
        "Balanced (q=82, m=4) — Recommended for JPG & PNG; noticeably smaller with crisp quality.",
        82,
        4,
        False,
    ),
    "high": (
        "High (q=90, m=5)",
        "High",
        "High (q=90, m=5) — High visual fidelity; smaller on PNG and most JPGs.",
        90,
        5,
        False,
    ),
    "max_lossy": (
        "Max Lossy (q=100, m=4)",
        "Max q=100",
        "Max Lossy (q=100, m=4) — Matches old cwebp -q 100; great on PNG, may be larger on JPG.",
        100,
        4,
        False,
    ),
    "lossless": (
        "Lossless (exact)",
        "Lossless",
        "Lossless (exact) — Pixel-identical for PNG/BMP; will be 2–5x larger than source JPG.",
        None,
        4,
        True,
    ),
    "custom": (
        "Custom",
        "Custom",
        "Custom — Using your manual quality, method, and lossless settings.",
        None,
        None,
        None,
    ),
}

STATUS_CELLS: Dict[str, Tuple[str, str]] = {
    "queued": ("·", NORD_DIM),
    "excluded": ("–", NORD_DIM),
    "running": ("⠋", NORD_FROST),
    "done": ("✔", NORD_GREEN),
    "skipped": ("↷", NORD_YELLOW),
    "failed": ("✖", NORD_RED),
}


@dataclass
class QueueItem:
    """One scanned image row in the TUI conversion queue."""

    src: str
    rel_path: str
    dst: str
    ext: str
    size_before: int
    size_after: int = 0
    included: bool = True
    status: str = "queued"  # queued | excluded | running | done | skipped | failed
    error: str = ""
    renamed: bool = False


def _windows_drives() -> List[str]:
    if not sys.platform.startswith("win"):
        return []
    if hasattr(os, "listdrives"):
        return list(os.listdrives())
    return [f"{ch}:\\" for ch in string.ascii_uppercase if os.path.exists(f"{ch}:\\")]


def _detect_preset(quality: int, method: int, lossless: bool) -> str:
    if lossless and method == 4:
        return "lossless"
    for key, (_, _, _, pq, pm, pl) in PRESETS.items():
        if key in ("custom", "lossless"):
            continue
        if quality == pq and method == pm and lossless == pl:
            return key
    return "custom"


def _status_text(status: str) -> Text:
    glyph, color = STATUS_CELLS.get(status, ("·", NORD_DIM))
    return Text(glyph, style=f"bold {color}")


def _delta_text(before: int, after: int) -> Text:
    if before <= 0 or after <= 0:
        return Text("—", style=NORD_DIM)
    pct = 100.0 * (after - before) / before
    color = NORD_GREEN if pct <= 0 else NORD_YELLOW
    return Text(f"{pct:+.1f}%", style=color)


class FolderTree(DirectoryTree):
    """DirectoryTree that only displays non-hidden folders and selects on icon/arrow clicks too."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        for path in paths:
            try:
                if path.is_dir() and not is_ignored_dir(path.name):
                    yield path
            except OSError:
                continue

    def _toggle_node(self, node) -> None:
        super()._toggle_node(node)
        if node.data is not None and self._safe_is_dir(node.data.path):
            self.post_message(self.DirectorySelected(node, node.data.path))


class ConfirmDeleteScreen(ModalScreen[bool]):
    """Confirmation dialog shown before deleting original files."""

    BINDINGS = [
        Binding("escape", "cancel_modal", "Cancel", show=True),
    ]

    def __init__(self, count: int) -> None:
        super().__init__()
        self._count = count

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label("Delete originals after conversion?", id="confirm-title")
            yield Static(
                f"{self._count} original image file(s) will be permanently deleted "
                "after their WebP output is saved.\n\nProceed?"
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="cancel-delete", variant="default")
                yield Button("Delete & Convert", id="confirm-delete", variant="error")

    def action_cancel_modal(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-delete")


class WebPConverterApp(App[None]):
    """Nord-themed studio TUI for batch WebP conversion."""

    TITLE = f"WebP Converter v{__version__}"
    SUB_TITLE = "Nord Studio"
    CSS_PATH = "tui.tcss"
    ENABLE_COMMAND_PALETTE = True

    BINDINGS = [
        Binding("ctrl+r", "start_convert", "Convert", show=True),
        Binding("ctrl+d", "start_dry_run", "Dry Run", show=True),
        Binding("b", "toggle_sidebar", "Sidebar", show=True),
        Binding("slash", "focus_filter", "Filter", show=True),
        Binding("1", "tab_queue", "Queue", show=False),
        Binding("2", "tab_settings", "Settings", show=False),
        Binding("3", "tab_log", "Log", show=False),
        Binding("space", "toggle_selected_row", "Toggle", show=False),
        Binding("escape", "escape_action", "Back / Cancel", show=True),
        Binding("ctrl+q", "quit_app", "Quit", show=True),
    ]

    def __init__(self, initial_folder: Optional[str] = None, config_file: Optional[str] = None) -> None:
        super().__init__()
        self.theme = "nord"
        self._config_file = config_file
        try:
            self._config = load_config(config_file)
            self._settings = settings_from_config(self._config)
        except (ValueError, TypeError):
            self._config = {}
            self._settings = Settings()

        raw_start = initial_folder if initial_folder else os.getcwd()
        self._initial_folder = normalize_folder_path(raw_start)
        self._active_folder: str = self._initial_folder
        self._cancel_event: Optional[threading.Event] = None
        self._scan_cancel: Optional[threading.Event] = None
        self._scan_id: int = 0
        self._busy = False
        self._shutting_down = False
        self._items: Dict[str, QueueItem] = {}
        self._item_order: List[str] = []
        self._empty_hint: str = ""

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand("Start conversion", "Convert queued images to WebP", self.action_start_convert)
        yield SystemCommand("Dry run", "Preview conversions in the Log tab without writing files", self.action_start_dry_run)
        yield SystemCommand("Retry failed files", "Re-run conversion on failed queue entries", self.action_retry_failed)
        yield SystemCommand("Toggle folder sidebar", "Show or hide the left directory browser", self.action_toggle_sidebar)
        yield SystemCommand("Save configuration", "Persist current settings to config.json", self._save_current_config)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="workspace"):
            with Vertical(id="left-pane", classes="card"):
                drives = _windows_drives()
                if drives:
                    with Horizontal(id="drive-row"):
                        for drv in drives:
                            label = drv.rstrip("\\")
                            yield Button(label, id=f"drive-{label[0]}", classes="drive-btn", compact=True)
                yield FolderTree(self._tree_root_for(self._initial_folder), id="folder-tree")

            with Vertical(id="right-pane"):
                with Vertical(id="path-card", classes="card"):
                    with Horizontal(id="path-row"):
                        yield Input(
                            value=self._initial_folder,
                            placeholder=r"C:\path\to\images",
                            id="folder-input",
                            compact=True,
                        )
                        yield Button("Open", id="go-btn", variant="default", compact=True)

                yield Static("Scanning folder...", id="summary-bar")

                with TabbedContent(initial="tab-queue", id="main-tabs"):
                    with TabPane("[1] Queue", id="tab-queue"):
                        with Horizontal(id="queue-toolbar"):
                            yield Label("Filter:", classes="toolbar-label")
                            yield Input(placeholder="name or .ext...", id="filter-input", compact=True)
                            yield Label("Preset:", classes="toolbar-label")
                            preset_options = [(label, key) for key, (label, *_) in PRESETS.items()]
                            initial_preset = _detect_preset(
                                self._settings.quality,
                                self._settings.method,
                                self._settings.lossless,
                            )
                            yield Select(
                                preset_options,
                                value=initial_preset,
                                allow_blank=False,
                                id="preset-select",
                                compact=True,
                            )
                        yield DataTable(id="queue-table", cursor_type="row", zebra_stripes=True)

                    with TabPane("[2] Settings", id="tab-settings"):
                        with VerticalScroll(id="settings-scroll"):
                            with Vertical(id="presets-section"):
                                yield Label("Presets (1-click)", id="presets-label")
                                with Horizontal(id="presets-row"):
                                    for pkey, (_, btn_label, _, _, _, _) in PRESETS.items():
                                        if pkey == "custom":
                                            continue
                                        yield Button(
                                            btn_label,
                                            id=f"preset-btn-{pkey}",
                                            classes="preset-btn",
                                            variant="primary" if pkey == initial_preset else "default",
                                            compact=True,
                                        )
                                yield Static(PRESETS[initial_preset][2], id="preset-hint")
                            with Horizontal(classes="setting-row"):
                                yield Label("Quality:", classes="field-label")
                                yield Input(value=str(self._settings.quality), id="quality-input", classes="num-input", compact=True)
                                yield Label("Method:", classes="field-label")
                                yield Input(value=str(self._settings.method), id="method-input", classes="num-input", compact=True)
                                yield Label("Workers:", classes="field-label")
                                yield Input(value=str(self._settings.max_workers), id="workers-input", classes="num-input", compact=True)
                            with Horizontal(classes="setting-row"):
                                yield Label("Lossless:", classes="switch-label")
                                yield Switch(value=self._settings.lossless, id="lossless-switch")
                            with Horizontal(classes="setting-row"):
                                yield Label("Include subfolders:", classes="switch-label")
                                yield Switch(value=self._settings.recursive, id="recursive-switch")
                            with Horizontal(classes="setting-row"):
                                yield Label("Overwrite .webp:", classes="switch-label")
                                yield Switch(value=self._settings.overwrite, id="overwrite-switch")
                            with Horizontal(classes="setting-row"):
                                yield Label("Delete originals:", classes="switch-label")
                                yield Switch(value=self._settings.delete_originals, id="delete-switch")

                            with Vertical(id="formats-section"):
                                yield Label("Supported Formats (Space or click to toggle)", id="formats-label")
                                all_formats = sorted(set(DEFAULT_SUPPORTED_FORMATS) | set(self._settings.supported_formats))
                                selections = [
                                    Selection(ext, ext, ext in self._settings.supported_formats)
                                    for ext in all_formats
                                ]
                                yield SelectionList[str](*selections, id="formats-list")

                            with Horizontal(id="settings-actions"):
                                yield Button("Save Config", id="save-cfg-btn", variant="primary", compact=True)

                    with TabPane("[3] Log", id="tab-log"):
                        yield RichLog(id="log-view", highlight=True, markup=True, wrap=True)

                with Vertical(id="progress-card", classes="card"):
                    with Horizontal(id="progress-row"):
                        yield ProgressBar(total=100, show_eta=False, id="progress-bar")
                        yield Static("Ready.", id="status-line")
                    with Horizontal(id="actions-row"):
                        yield Button("Convert", id="convert-btn", variant="primary", compact=True)
                        yield Button("Dry Run", id="dry-run-btn", variant="default", compact=True)
                        yield Button("Retry", id="retry-btn", variant="warning", disabled=True, compact=True)
                        yield Button("Cancel", id="cancel-btn", variant="error", disabled=True, compact=True)
        yield Footer()

    def on_mount(self) -> None:
        left = self.query_one("#left-pane")
        left.border_title = "Folders"
        left.border_subtitle = "B to hide"

        path_card = self.query_one("#path-card")
        path_card.border_title = "Target Folder"

        progress_card = self.query_one("#progress-card")
        progress_card.border_title = "Batch Control"

        table = self.query_one("#queue-table", DataTable)
        table.add_column("Inc", key="sel", width=5)
        table.add_column("St", key="status", width=4)
        table.add_column("File", key="file")
        table.add_column("Output", key="output")
        table.add_column("Before", key="before", width=10)
        table.add_column("After", key="after", width=10)
        table.add_column("Delta", key="delta", width=14)

        self._sync_lossless_state()
        self._trigger_scan(self._active_folder)

    @staticmethod
    def _tree_root_for(folder: str) -> Path:
        try:
            p = Path(normalize_folder_path(folder)).resolve()
        except OSError:
            return Path.home()
        if not p.exists():
            return Path.home()
        return p.parent if p.parent != p else p

    def _safe_call_from_thread(self, fn, *args) -> None:
        """Invoke a UI callback from a worker thread safely, ignoring teardown races."""
        if self._shutting_down or not self.is_running:
            return
        try:
            self.call_from_thread(fn, *args)
        except Exception:
            pass

    def _sync_lossless_state(self) -> None:
        if not self.is_mounted:
            return
        lossless = self.query_one("#lossless-switch", Switch).value
        self.query_one("#quality-input", Input).disabled = self._busy or lossless

    def _current_formats(self) -> Set[str]:
        return set(self.query_one("#formats-list", SelectionList).selected)

    def _read_settings(self) -> Optional[Settings]:
        """Validate UI inputs into a Settings object, notifying on invalid values."""
        lossless = self.query_one("#lossless-switch", Switch).value
        try:
            if lossless:
                # Quality is unused in lossless mode; don't let an empty/disabled input block the user
                raw_q = self.query_one("#quality-input", Input).value.strip()
                try:
                    quality = int(raw_q)
                    if not 0 <= quality <= 100:
                        quality = self._settings.quality
                except ValueError:
                    quality = self._settings.quality
            else:
                quality = int(self.query_one("#quality-input", Input).value.strip())
                if not 0 <= quality <= 100:
                    raise ValueError("Quality must be between 0 and 100.")
            method = int(self.query_one("#method-input", Input).value.strip())
            if not 0 <= method <= 6:
                raise ValueError("Method must be between 0 and 6.")
            workers = int(self.query_one("#workers-input", Input).value.strip())
            if workers < 1:
                raise ValueError("Workers must be at least 1.")
            formats = self._current_formats()
            if not formats:
                raise ValueError("Select at least one image format in Settings.")
        except ValueError as e:
            self.notify(str(e), title="Invalid Setting", severity="error")
            self.query_one("#log-view", RichLog).write(f"[{NORD_RED}]Invalid setting:[/{NORD_RED}] {escape(str(e))}")
            return None

        return Settings(
            quality=quality,
            lossless=lossless,
            method=method,
            delete_originals=self.query_one("#delete-switch", Switch).value,
            overwrite=self.query_one("#overwrite-switch", Switch).value,
            recursive=self.query_one("#recursive-switch", Switch).value,
            max_workers=workers,
            supported_formats=formats,
        )

    def _set_running(self, running: bool) -> None:
        self._busy = running
        lock_ids = (
            "#convert-btn",
            "#dry-run-btn",
            "#save-cfg-btn",
            "#go-btn",
            "#folder-input",
            "#preset-select",
            "#folder-tree",
            "#method-input",
            "#workers-input",
            "#lossless-switch",
            "#recursive-switch",
            "#overwrite-switch",
            "#delete-switch",
            "#formats-list",
        )
        for wid in lock_ids:
            self.query_one(wid).disabled = running
        for btn in self.query(".drive-btn"):
            btn.disabled = running
        for btn in self.query(".preset-btn"):
            btn.disabled = running
        self._sync_lossless_state()
        self.query_one("#cancel-btn", Button).disabled = not running
        self._update_retry_button()

    def _update_retry_button(self) -> None:
        has_failed = any(i.status == "failed" for i in self._items.values())
        self.query_one("#retry-btn", Button).disabled = self._busy or not has_failed

    def _update_summary_bar(self) -> None:
        total = len(self._items)
        if total == 0:
            hint = self._empty_hint or "No supported images in this folder — pick another folder or check Formats in Settings."
            self.query_one("#summary-bar", Static).update(f"[{NORD_DIM}]{escape(hint)}[/{NORD_DIM}]")
            return

        selected = [i for i in self._items.values() if i.included and i.status != "skipped"]
        done = [i for i in self._items.values() if i.status == "done"]
        failed = sum(1 for i in self._items.values() if i.status == "failed")
        skipped = sum(1 for i in self._items.values() if i.status == "skipped")

        total_before = sum(i.size_before for i in selected)
        if done:
            done_before = sum(i.size_before for i in done)
            done_after = sum(i.size_after for i in done)
            saved = done_before - done_after
            pct = (100.0 * saved / done_before) if done_before else 0.0
            dir_word = "saved" if saved >= 0 else "larger"
            color = NORD_GREEN if saved >= 0 else NORD_YELLOW
            size_part = (
                f"{format_bytes(done_before)} -> {format_bytes(done_after)} "
                f"([{color}]{format_bytes(abs(saved))} {dir_word}, {abs(pct):.1f}%[/{color}])"
            )
        else:
            size_part = f"Input: [bold]{format_bytes(total_before)}[/bold]"

        err_part = f"[{NORD_RED}]{failed} failed[/{NORD_RED}]" if failed else f"[{NORD_DIM}]0 failed[/{NORD_DIM}]"
        skip_part = f" │ [{NORD_YELLOW}]{skipped} conflicts[/{NORD_YELLOW}]" if skipped else ""
        self.query_one("#summary-bar", Static).update(
            f"[bold {NORD_FROST}]{len(selected)}/{total}[/] queued │ {size_part}{skip_part} │ {err_part}"
        )

    def _cancel_active_scan(self) -> int:
        if self._scan_cancel is not None:
            self._scan_cancel.set()
        self._scan_id += 1
        return self._scan_id

    def _update_scope_subtitle(self) -> None:
        recursive = self.query_one("#recursive-switch", Switch).value
        self.query_one("#path-card").border_subtitle = "Including subfolders" if recursive else "Selected folder only"

    def _reset_progress_display(self) -> None:
        self.query_one("#progress-bar", ProgressBar).update(total=100, progress=0)
        self.query_one("#status-line", Static).update("Ready.")

    def _trigger_scan(self, target_folder: Optional[str] = None) -> None:
        if self._busy or self._shutting_down:
            return
        scan_id = self._cancel_active_scan()
        cancel_event = threading.Event()
        self._scan_cancel = cancel_event

        raw_path = target_folder if target_folder is not None else self._active_folder
        recursive = self.query_one("#recursive-switch", Switch).value
        overwrite = self.query_one("#overwrite-switch", Switch).value
        formats = self._current_formats()

        # Clear stale items immediately so Ctrl+R cannot convert a previous folder while scanning
        self._items.clear()
        self._item_order.clear()
        self.query_one("#queue-table", DataTable).clear()
        self._update_retry_button()
        self._reset_progress_display()
        self._update_scope_subtitle()
        self.query_one("#summary-bar", Static).update(f"[{NORD_FROST}]Scanning folder...[/{NORD_FROST}]")
        self._scan_worker(scan_id, raw_path, recursive, formats, overwrite, cancel_event)

    @work(thread=True, exclusive=True, group="scan")
    def _scan_worker(
        self,
        scan_id: int,
        raw_path: str,
        recursive: bool,
        formats: Set[str],
        overwrite: bool,
        cancel_event: threading.Event,
    ) -> None:
        folder, err = check_folder(raw_path)
        if cancel_event.is_set() or self._shutting_down:
            return
        if err or not folder:
            self._safe_call_from_thread(self._apply_scan_error, scan_id, err or "Invalid folder")
            return
        if not formats:
            self._safe_call_from_thread(self._apply_scan_results, scan_id, folder, [])
            return

        try:
            files = scan_folder(folder, formats, recursive=recursive, cancel_event=cancel_event)
        except OSError as e:
            if not cancel_event.is_set():
                self._safe_call_from_thread(self._apply_scan_error, scan_id, f"Cannot read folder: {e}")
            return

        if cancel_event.is_set() or self._shutting_down:
            return
        plan, skipped = plan_conversions(files, overwrite=overwrite)
        plan_map = dict(plan)
        skipped_set = set(skipped)

        items: List[QueueItem] = []
        for src in files:
            if cancel_event.is_set() or self._shutting_down:
                return
            try:
                size = os.path.getsize(src)
            except OSError:
                size = 0
            rel = os.path.relpath(src, folder)
            ext = os.path.splitext(src)[1].lower()
            default_dst = os.path.splitext(src)[0] + ".webp"
            if src in skipped_set:
                items.append(
                    QueueItem(
                        src=src,
                        rel_path=rel,
                        dst="",
                        ext=ext,
                        size_before=size,
                        included=False,
                        status="skipped",
                        error="Output filename conflict",
                    )
                )
            else:
                dst = plan_map.get(src, default_dst)
                renamed = os.path.normcase(dst) != os.path.normcase(default_dst)
                items.append(
                    QueueItem(
                        src=src,
                        rel_path=rel,
                        dst=dst,
                        ext=ext,
                        size_before=size,
                        included=True,
                        status="queued",
                        renamed=renamed,
                    )
                )
        if not cancel_event.is_set() and not self._shutting_down:
            empty_hint = ""
            if not items:
                empty_hint = self._describe_empty_folder(folder, recursive)
            self._safe_call_from_thread(self._apply_scan_results, scan_id, folder, items, empty_hint)

    @staticmethod
    def _describe_empty_folder(folder: str, recursive: bool) -> str:
        subdirs = 0
        webp_count = 0
        other_exts: Set[str] = set()
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not is_ignored_dir(entry.name):
                                subdirs += 1
                        elif entry.is_file(follow_symlinks=True):
                            ext = os.path.splitext(entry.name)[1].lower()
                            if ext == ".webp":
                                webp_count += 1
                            elif ext:
                                other_exts.add(ext)
                    except OSError:
                        continue
        except OSError:
            pass

        parts: List[str] = []
        if not recursive and subdirs:
            parts.append(f"{subdirs} subfolder(s) — open one or enable Include subfolders in Settings")
        if webp_count:
            parts.append(f"{webp_count} already .webp")
        if not parts and other_exts:
            sample = ", ".join(sorted(other_exts)[:5])
            parts.append(f"found {sample} — check Formats in Settings")
        if parts:
            return "No matching images (" + "; ".join(parts) + ")"
        return "No supported images in this folder."

    def _apply_scan_error(self, scan_id: int, err: str) -> None:
        if self._shutting_down or not self.is_mounted or self._busy or scan_id != self._scan_id:
            return
        self._items.clear()
        self._item_order.clear()
        self._empty_hint = ""
        self.query_one("#queue-table", DataTable).clear()
        self.query_one("#summary-bar", Static).update(f"[{NORD_RED}]{escape(err)}[/{NORD_RED}]")
        self._update_retry_button()

    def _apply_scan_results(self, scan_id: int, folder: str, items: List[QueueItem], empty_hint: str = "") -> None:
        if self._shutting_down or not self.is_mounted or self._busy or scan_id != self._scan_id:
            return
        self._active_folder = folder
        self._items = {item.src: item for item in items}
        self._item_order = [item.src for item in items]
        self._empty_hint = empty_hint
        self._rebuild_table()
        self._update_summary_bar()
        self._update_retry_button()

    def _row_cells(self, item: QueueItem) -> Tuple[Text, Text, Text, Text, Text, Text, Text]:
        sel = Text("[x]", style=NORD_FROST) if item.included else Text("[ ]", style=NORD_DIM)
        st = _status_text(item.status)
        file_cell = Text(item.rel_path, style=NORD_DIM if not item.included else "")
        if item.dst:
            out_name = os.path.basename(item.dst)
            out_cell = Text(out_name, style=NORD_YELLOW if item.renamed else NORD_BLUE)
        else:
            out_cell = Text("(conflict)", style=NORD_YELLOW)
        before_cell = Text(format_bytes(item.size_before))
        after_cell = Text(format_bytes(item.size_after)) if item.size_after else Text("—", style=NORD_DIM)
        delta_cell = (
            Text(item.error[:12], style=NORD_RED)
            if item.status == "failed" and item.error
            else _delta_text(item.size_before, item.size_after)
        )
        return sel, st, file_cell, out_cell, before_cell, after_cell, delta_cell

    def _rebuild_table(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        table.clear()
        needle = self.query_one("#filter-input", Input).value.strip().lower()
        for src in self._item_order:
            item = self._items[src]
            if needle and needle not in item.rel_path.lower() and needle not in item.ext:
                continue
            table.add_row(*self._row_cells(item), key=item.src)

    def _refresh_table_row(self, item: QueueItem) -> None:
        table = self.query_one("#queue-table", DataTable)
        if item.src not in table.rows:
            return
        sel, st, file_cell, out_cell, before_cell, after_cell, delta_cell = self._row_cells(item)
        table.update_cell(item.src, "sel", sel)
        table.update_cell(item.src, "status", st)
        table.update_cell(item.src, "file", file_cell)
        table.update_cell(item.src, "output", out_cell)
        table.update_cell(item.src, "before", before_cell)
        table.update_cell(item.src, "after", after_cell)
        table.update_cell(item.src, "delta", delta_cell)

    def _update_preset_ui(self, active_key: str) -> None:
        if not self.is_mounted:
            return
        sel = self.query_one("#preset-select", Select)
        if sel.value != active_key:
            with sel.prevent(Select.Changed):
                sel.value = active_key
        for pkey in PRESETS:
            if pkey == "custom":
                continue
            btn = self.query_one(f"#preset-btn-{pkey}", Button)
            btn.variant = "primary" if pkey == active_key else "default"
        hint = PRESETS.get(active_key, PRESETS["custom"])[2]
        self.query_one("#preset-hint", Static).update(hint)

    def _apply_preset(self, key: str) -> None:
        if key not in PRESETS or key == "custom" or self._busy:
            return
        _, _, _, pq, pm, pl = PRESETS[key]
        q_input = self.query_one("#quality-input", Input)
        m_input = self.query_one("#method-input", Input)
        l_switch = self.query_one("#lossless-switch", Switch)
        if pq is not None:
            with q_input.prevent(Input.Changed):
                q_input.value = str(pq)
        if pm is not None:
            with m_input.prevent(Input.Changed):
                m_input.value = str(pm)
        if pl is not None:
            with l_switch.prevent(Switch.Changed):
                l_switch.value = pl
        self._sync_lossless_state()
        self._update_preset_ui(key)

    def _sync_preset_selector_from_inputs(self) -> None:
        if not self.is_mounted:
            return
        lossless = self.query_one("#lossless-switch", Switch).value
        try:
            m = int(self.query_one("#method-input", Input).value.strip())
            q = 100 if lossless else int(self.query_one("#quality-input", Input).value.strip())
            detected = _detect_preset(q, m, lossless)
        except ValueError:
            detected = "custom"
        self._update_preset_ui(detected)

    # ------------------------------------------------------------ UI Events

    def _clear_filter_silent(self) -> None:
        flt = self.query_one("#filter-input", Input)
        if flt.value:
            with flt.prevent(Input.Changed):
                flt.value = ""

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        if self._busy:
            return
        folder = normalize_folder_path(str(event.path))
        self.query_one("#folder-input", Input).value = folder
        if folder != self._active_folder:
            self._clear_filter_silent()
        self._active_folder = folder
        self._trigger_scan(folder)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "preset-select" or self._busy:
            return
        self._apply_preset(str(event.value))

    def on_switch_changed(self, event: Switch.Changed) -> None:
        sid = event.switch.id
        if sid == "lossless-switch":
            self._sync_lossless_state()
            self._sync_preset_selector_from_inputs()
        elif sid in ("recursive-switch", "overwrite-switch") and not self._busy:
            self._trigger_scan(self._active_folder)

    def on_input_changed(self, event: Input.Changed) -> None:
        iid = event.input.id
        if iid == "filter-input":
            self._rebuild_table()
        elif iid in ("quality-input", "method-input"):
            self._sync_preset_selector_from_inputs()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        iid = event.input.id
        if iid == "folder-input" and not self._busy:
            self._open_typed_folder()
            if self.query_one("#main-tabs", TabbedContent).active == "tab-queue":
                self.query_one("#queue-table", DataTable).focus()
            else:
                self.set_focus(None)
        elif iid == "filter-input":
            self.query_one("#queue-table", DataTable).focus()
        else:
            self.set_focus(None)

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        if event.selection_list.id == "formats-list" and not self._busy:
            self._trigger_scan(self._active_folder)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        src = str(event.row_key.value)
        item = self._items.get(src)
        if not item:
            return
        if item.status == "failed" and item.error:
            self.notify(f"{item.rel_path}: {item.error}", title="Conversion Error", severity="error")
        elif not self._busy and item.status != "skipped":
            self._toggle_item(item)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("drive-") and not self._busy:
            drive_root = f"{bid.split('-', 1)[1]}:\\"
            tree = self.query_one("#folder-tree", FolderTree)
            tree.path = Path(drive_root)
            tree.focus()
            self.notify(f"Browsing {drive_root} — select a folder to load its images", title="Drive Switched")
        elif bid.startswith("preset-btn-") and not self._busy:
            self._apply_preset(bid.removeprefix("preset-btn-"))
        elif bid == "go-btn":
            self._open_typed_folder()
        elif bid == "convert-btn":
            self.action_start_convert()
        elif bid == "dry-run-btn":
            self.action_start_dry_run()
        elif bid == "retry-btn":
            self.action_retry_failed()
        elif bid == "save-cfg-btn":
            self._save_current_config()
        elif bid == "cancel-btn":
            self.action_cancel_batch()

    # ------------------------------------------------------------ Actions

    def _open_typed_folder(self) -> None:
        raw = self.query_one("#folder-input", Input).value
        folder, err = check_folder(raw)
        if err or not folder:
            scan_id = self._cancel_active_scan()
            self._apply_scan_error(scan_id, err or "Invalid folder")
            self.notify(err or "Invalid folder", severity="error")
            return
        self.query_one("#folder-input", Input).value = folder
        if folder != self._active_folder:
            self._clear_filter_silent()
        self._active_folder = folder
        self.query_one("#folder-tree", FolderTree).path = self._tree_root_for(folder)
        self._trigger_scan(folder)

    def _toggle_item(self, item: QueueItem) -> None:
        item.included = not item.included
        item.status = "queued" if item.included else "excluded"
        item.size_after = 0
        item.error = ""
        self._refresh_table_row(item)
        self._update_summary_bar()
        self._update_retry_button()

    def action_toggle_selected_row(self) -> None:
        if self._busy:
            return
        table = self.query_one("#queue-table", DataTable)
        if not table.has_focus or table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        item = self._items.get(str(row_key.value))
        if item and item.status != "skipped":
            self._toggle_item(item)

    def action_toggle_sidebar(self) -> None:
        pane = self.query_one("#left-pane")
        pane.toggle_class("hidden")
        if pane.has_class("hidden") and self.focused is not None and pane in self.focused.ancestors_with_self:
            self.query_one("#queue-table", DataTable).focus()

    def action_focus_filter(self) -> None:
        self.query_one("#main-tabs", TabbedContent).active = "tab-queue"
        self.query_one("#filter-input", Input).focus()

    def action_tab_queue(self) -> None:
        self.query_one("#main-tabs", TabbedContent).active = "tab-queue"

    def action_tab_settings(self) -> None:
        self.query_one("#main-tabs", TabbedContent).active = "tab-settings"

    def action_tab_log(self) -> None:
        self.query_one("#main-tabs", TabbedContent).active = "tab-log"

    def action_escape_action(self) -> None:
        if self._busy:
            self.action_cancel_batch()
            return
        flt = self.query_one("#filter-input", Input)
        if flt.has_focus:
            if flt.value:
                flt.value = ""  # on_input_changed rebuilds the table
            self.query_one("#queue-table", DataTable).focus()
            return
        if isinstance(self.focused, Input):
            # Stay on the active tab (don't yank the user out of Settings)
            self.set_focus(None)
            return
        if flt.value:
            flt.value = ""

    def action_cancel_batch(self) -> None:
        if self._busy and self._cancel_event:
            self._cancel_event.set()
            self.query_one("#status-line", Static).update("Cancelling after active files finish...")

    def action_quit_app(self) -> None:
        self._shutting_down = True
        if self._scan_cancel:
            self._scan_cancel.set()
        if self._cancel_event:
            self._cancel_event.set()
        self.exit()

    def _save_current_config(self) -> None:
        if self._busy:
            return
        settings = self._read_settings()
        if not settings:
            return
        saved_to = save_config(settings, self._config_file)
        log = self.query_one("#log-view", RichLog)
        if saved_to:
            self._settings = settings
            log.write(f"[{NORD_GREEN}]Settings saved to[/{NORD_GREEN}] {escape(saved_to)}")
            self.notify(f"Saved to {os.path.basename(saved_to)}", title="Config Saved")
        else:
            log.write(f"[{NORD_RED}]Failed to save settings.[/{NORD_RED}]")
            self.notify("Could not write config.json", severity="error")

    def action_start_dry_run(self) -> None:
        self._launch(dry_run=True, failed_only=False)

    def action_start_convert(self) -> None:
        self._launch(dry_run=False, failed_only=False)

    def action_retry_failed(self) -> None:
        self._launch(dry_run=False, failed_only=True)

    def _launch(self, dry_run: bool, failed_only: bool) -> None:
        if self._busy or self._shutting_down:
            return
        settings = self._read_settings()
        if not settings:
            return

        # Cancel any in-flight background scan before planning
        self._cancel_active_scan()

        log = self.query_one("#log-view", RichLog)
        if failed_only:
            candidates = [i for i in self._items.values() if i.status == "failed"]
        else:
            candidates = [i for i in self._items.values() if i.included and i.status != "skipped"]

        # Prune sources that were already deleted (e.g., from a previous delete_originals run)
        existing_sources = [i.src for i in candidates if os.path.isfile(i.src)]
        missing_sources = [i.src for i in candidates if not os.path.isfile(i.src)]
        if missing_sources:
            for src in missing_sources:
                self._items.pop(src, None)
            self._item_order = [s for s in self._item_order if s in self._items]
            self._rebuild_table()
            self._update_summary_bar()
            self._update_retry_button()

        if not existing_sources:
            msg = (
                "No failed files to retry."
                if failed_only
                else "No remaining source images to convert (originals may have been deleted)."
                if missing_sources
                else "No queued images to convert."
            )
            self.notify(msg, severity="warning")
            log.write(f"[{NORD_YELLOW}]{escape(msg)}[/{NORD_YELLOW}]")
            return

        # Re-plan against current disk state so overwrite=False is always respected on re-runs
        plan, skipped = plan_conversions(existing_sources, overwrite=settings.overwrite)
        plan_map = dict(plan)
        for src in existing_sources:
            item = self._items.get(src)
            if not item:
                continue
            if src in plan_map:
                default_dst = os.path.splitext(src)[0] + ".webp"
                item.dst = plan_map[src]
                item.renamed = os.path.normcase(item.dst) != os.path.normcase(default_dst)
            else:
                item.dst = ""
                item.status = "skipped"
                item.included = False
                item.error = "Output filename conflict"
            self._refresh_table_row(item)

        if not plan:
            msg = "Nothing to convert (all files skipped due to output conflicts)."
            self.notify(msg, severity="warning")
            log.write(f"[{NORD_YELLOW}]{escape(msg)}[/{NORD_YELLOW}]")
            return

        if dry_run:
            log.clear()
            log.write(f"[{NORD_FROST}]Dry run:[/{NORD_FROST}] {len(plan)} file(s) to convert, {len(skipped)} skipped.")
            for src, dst in plan:
                log.write(f"  {escape(src)} -> {escape(os.path.basename(dst))}")
            self.query_one("#main-tabs", TabbedContent).active = "tab-log"
            self.notify(f"Dry run: {len(plan)} file(s) ready", title="Dry Run")
            return

        if settings.delete_originals:
            captured_folder = self._active_folder

            def on_confirm(confirmed: Optional[bool]) -> None:
                if confirmed and not self._busy and not self._shutting_down and self._active_folder == captured_folder:
                    self._begin_worker(settings, plan, skipped)

            self.push_screen(ConfirmDeleteScreen(len(plan)), on_confirm)
        else:
            self._begin_worker(settings, plan, skipped)

    def _begin_worker(self, settings: Settings, plan: List[Tuple[str, str]], skipped: List[str]) -> None:
        self._cancel_active_scan()
        log = self.query_one("#log-view", RichLog)
        log.clear()
        mode = "lossless" if settings.lossless else f"quality={settings.quality}"
        log.write(
            f"[{NORD_FROST}]Starting:[/{NORD_FROST}] {len(plan)} file(s), {mode}, "
            f"method={settings.method}, workers={settings.max_workers}"
        )
        if skipped:
            log.write(f"[{NORD_YELLOW}]Skipped {len(skipped)} file(s) due to name conflicts.[/{NORD_YELLOW}]")

        for src, _ in plan:
            if src in self._items:
                item = self._items[src]
                item.status = "queued"
                item.size_after = 0
                item.error = ""
                self._refresh_table_row(item)

        bar = self.query_one("#progress-bar", ProgressBar)
        bar.update(total=len(plan), progress=0)
        self.query_one("#status-line", Static).update(f"0/{len(plan)}...")
        self.query_one("#main-tabs", TabbedContent).active = "tab-queue"
        self._cancel_event = threading.Event()
        self._set_running(True)
        self._run_worker(settings, plan, skipped, self._cancel_event)

    @work(thread=True)
    def _run_worker(
        self,
        settings: Settings,
        plan: List[Tuple[str, str]],
        skipped: List[str],
        cancel_event: threading.Event,
    ) -> None:
        def on_event(ev: BatchEvent) -> None:
            self._safe_call_from_thread(self._handle_batch_event, ev)

        run_batch(settings, plan, skipped=skipped, on_event=on_event, cancel_event=cancel_event)

    def _handle_batch_event(self, ev: BatchEvent) -> None:
        if self._shutting_down or not self.is_mounted:
            return
        log = self.query_one("#log-view", RichLog)
        bar = self.query_one("#progress-bar", ProgressBar)
        status = self.query_one("#status-line", Static)

        if ev.kind == "file_started":
            item = self._items.get(ev.src)
            if item:
                item.status = "running"
                self._refresh_table_row(item)
            status.update(f"{ev.completed}/{ev.total} • {escape(os.path.basename(ev.src))}")

        elif ev.kind == "file_completed":
            bar.update(progress=ev.completed)
            item = self._items.get(ev.src)
            if item:
                item.status = "done"
                item.size_after = ev.bytes_after
                item.error = ""
                self._refresh_table_row(item)
            pct = (100.0 * (ev.bytes_after - ev.bytes_before) / ev.bytes_before) if ev.bytes_before else 0.0
            status.update(f"{ev.completed}/{ev.total} • {escape(os.path.basename(ev.dst))}")
            log.write(
                f"[{NORD_GREEN}]OK[/{NORD_GREEN}] {escape(os.path.basename(ev.src))} -> {escape(os.path.basename(ev.dst))} "
                f"({format_bytes(ev.bytes_before)} -> {format_bytes(ev.bytes_after)}, {pct:+.1f}%)"
            )
            self._update_summary_bar()

        elif ev.kind == "file_failed":
            bar.update(progress=ev.completed)
            item = self._items.get(ev.src)
            if item:
                item.status = "failed"
                item.size_after = ev.bytes_after
                item.error = ev.error
                self._refresh_table_row(item)
            status.update(f"{ev.completed}/{ev.total} • failed: {escape(os.path.basename(ev.src))}")
            log.write(f"[{NORD_RED}]FAIL[/{NORD_RED}] {escape(ev.src)}: {escape(ev.error)}")
            self._update_summary_bar()

        elif ev.kind == "batch_finished" and ev.summary is not None:
            s = ev.summary
            self._set_running(False)
            saved = s.saved_bytes
            pct = (100.0 * saved / s.bytes_before) if s.bytes_before else 0.0
            label = "Cancelled" if s.cancelled else "Done"
            direction = "saved" if saved >= 0 else "larger"
            summary_text = (
                f"{label}: {s.succeeded} ok, {s.failed} failed, {s.skipped} skipped "
                f"({format_bytes(abs(saved))} {direction}, {abs(pct):.1f}%)"
            )
            status.update(escape(summary_text))
            color = NORD_YELLOW if (s.failed or s.skipped or s.cancelled) else NORD_GREEN
            log.write(f"[{color}]{escape(summary_text)}[/{color}]")
            self._update_summary_bar()
            severity = "warning" if (s.failed or s.cancelled) else "information"
            self.notify(summary_text, title=label, severity=severity)


def run_tui(initial_folder: Optional[str] = None, config_file: Optional[str] = None) -> None:
    """Launch the interactive Textual TUI."""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    root.addHandler(logging.NullHandler())

    WebPConverterApp(initial_folder=initial_folder, config_file=config_file).run()
