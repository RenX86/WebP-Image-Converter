"""Interactive terminal UI (TUI) powered by Textual."""

import logging
import os
from pathlib import Path
import threading
from typing import Iterable, Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Static,
    Switch,
)

from . import __version__
from .cli import format_bytes
from .config import Settings, load_config, save_config, settings_from_config
from .core import BatchEvent, check_folder, plan_conversions, run_batch, scan_folder


class FolderTree(DirectoryTree):
    """DirectoryTree that only displays folders for fast navigation."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        for path in paths:
            try:
                if path.is_dir():
                    yield path
            except OSError:
                continue


class ConfirmDeleteScreen(ModalScreen[bool]):
    """Confirmation dialog shown before deleting original files."""

    DEFAULT_CSS = """
    ConfirmDeleteScreen {
        align: center middle;
    }
    #confirm-dialog {
        width: 58;
        height: auto;
        padding: 1 2;
        border: thick $error;
        background: $surface;
    }
    #confirm-title {
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }
    #confirm-buttons {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    #confirm-buttons Button {
        margin-left: 1;
    }
    """

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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-delete")


class WebPConverterApp(App[None]):
    """Interactive Textual dashboard for batch WebP conversion."""

    TITLE = f"WebP Converter v{__version__}"
    SUB_TITLE = "Terminal UI"

    CSS = """
    Screen {
        layout: vertical;
    }
    #workspace {
        height: 1fr;
    }
    #left-pane {
        width: 36;
        min-width: 28;
        border: round $primary;
        padding: 0 1;
    }
    #right-pane {
        width: 1fr;
        padding: 0 1;
    }
    .pane-title {
        text-style: bold;
        color: $accent;
        padding: 0 0 1 0;
    }
    #folder-tree {
        height: 1fr;
    }
    #path-row {
        height: auto;
        margin-bottom: 1;
    }
    #folder-input {
        width: 1fr;
    }
    #go-btn {
        width: 8;
        margin-left: 1;
    }
    #preview-label {
        color: $text-muted;
        height: auto;
        margin-bottom: 1;
    }
    #settings-box {
        height: auto;
        border: round $primary-darken-2;
        padding: 1;
        margin-bottom: 1;
    }
    .setting-row {
        height: 3;
        align-vertical: middle;
    }
    .field-label {
        width: 11;
        content-align: left middle;
    }
    .num-input {
        width: 10;
        margin-right: 2;
    }
    .switch-label {
        width: 18;
        content-align: left middle;
    }
    Switch {
        margin-right: 2;
    }
    #actions {
        height: auto;
        margin-bottom: 1;
    }
    #actions Button {
        margin-right: 1;
    }
    #progress-box {
        height: auto;
        margin-bottom: 1;
    }
    #status-line {
        color: $text-muted;
        height: 1;
    }
    #log-view {
        height: 1fr;
        border: round $surface-lighten-2;
    }
    """

    BINDINGS = [
        Binding("ctrl+r", "start_convert", "Convert", show=True),
        Binding("ctrl+d", "start_dry_run", "Dry Run", show=True),
        Binding("escape", "cancel_batch", "Cancel", show=True),
        Binding("ctrl+q", "quit_app", "Quit", show=True),
    ]

    def __init__(self, initial_folder: Optional[str] = None) -> None:
        super().__init__()
        self._config = load_config()
        try:
            self._settings = settings_from_config(self._config)
        except ValueError:
            self._settings = Settings()
        self._initial_folder = initial_folder or os.getcwd()
        self._cancel_event: Optional[threading.Event] = None
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="workspace"):
            with Vertical(id="left-pane"):
                yield Label("Folders", classes="pane-title")
                start_root = self._tree_root_for(self._initial_folder)
                yield FolderTree(start_root, id="folder-tree")
            with Vertical(id="right-pane"):
                yield Label("Target Folder", classes="pane-title")
                with Horizontal(id="path-row"):
                    yield Input(value=self._initial_folder, placeholder=r"C:\path\to\images", id="folder-input")
                    yield Button("Open", id="go-btn", variant="default")
                yield Static("Select a folder to preview images...", id="preview-label")

                with Vertical(id="settings-box"):
                    with Horizontal(classes="setting-row"):
                        yield Label("Quality:", classes="field-label")
                        yield Input(value=str(self._settings.quality), id="quality-input", classes="num-input")
                        yield Label("Method:", classes="field-label")
                        yield Input(value=str(self._settings.method), id="method-input", classes="num-input")
                        yield Label("Workers:", classes="field-label")
                        yield Input(value=str(self._settings.max_workers), id="workers-input", classes="num-input")
                    with Horizontal(classes="setting-row"):
                        yield Label("Lossless:", classes="switch-label")
                        yield Switch(value=self._settings.lossless, id="lossless-switch")
                        yield Label("Recursive:", classes="switch-label")
                        yield Switch(value=self._settings.recursive, id="recursive-switch")
                    with Horizontal(classes="setting-row"):
                        yield Label("Overwrite .webp:", classes="switch-label")
                        yield Switch(value=self._settings.overwrite, id="overwrite-switch")
                        yield Label("Delete originals:", classes="switch-label")
                        yield Switch(value=self._settings.delete_originals, id="delete-switch")

                with Horizontal(id="actions"):
                    yield Button("Convert", id="convert-btn", variant="primary")
                    yield Button("Dry Run", id="dry-run-btn", variant="default")
                    yield Button("Save Config", id="save-cfg-btn", variant="default")
                    yield Button("Cancel", id="cancel-btn", variant="error", disabled=True)

                with Vertical(id="progress-box"):
                    yield Static("Ready.", id="status-line")
                    yield ProgressBar(total=100, show_eta=False, id="progress-bar")

                yield RichLog(id="log-view", highlight=True, markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._sync_lossless_state()
        self._refresh_preview(self._initial_folder, self._settings.recursive)

    @staticmethod
    def _tree_root_for(folder: str) -> Path:
        p = Path(folder)
        if not p.exists():
            return Path.home()
        # Start at the drive/anchor on Windows if inside a shallow folder, else parent
        return p.parent if p.parent != p else p

    def _sync_lossless_state(self) -> None:
        lossless = self.query_one("#lossless-switch", Switch).value
        self.query_one("#quality-input", Input).disabled = lossless

    def _read_settings(self) -> Optional[Settings]:
        """Validate UI inputs into a Settings object, logging errors on bad values."""
        log = self.query_one("#log-view", RichLog)
        try:
            quality = int(self.query_one("#quality-input", Input).value.strip())
            if not 0 <= quality <= 100:
                raise ValueError("Quality must be between 0 and 100.")
            method = int(self.query_one("#method-input", Input).value.strip())
            if not 0 <= method <= 6:
                raise ValueError("Method must be between 0 and 6.")
            workers = int(self.query_one("#workers-input", Input).value.strip())
            if workers < 1:
                raise ValueError("Workers must be at least 1.")
        except ValueError as e:
            log.write(f"[red]Invalid setting:[/red] {e}")
            return None

        return Settings(
            quality=quality,
            lossless=self.query_one("#lossless-switch", Switch).value,
            method=method,
            delete_originals=self.query_one("#delete-switch", Switch).value,
            overwrite=self.query_one("#overwrite-switch", Switch).value,
            recursive=self.query_one("#recursive-switch", Switch).value,
            max_workers=workers,
            supported_formats=set(self._settings.supported_formats),
        )

    def _set_running(self, running: bool) -> None:
        self._busy = running
        for widget_id in ("#convert-btn", "#dry-run-btn", "#save-cfg-btn", "#go-btn", "#folder-input"):
            self.query_one(widget_id).disabled = running
        self.query_one("#cancel-btn", Button).disabled = not running

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        if self._busy:
            return
        folder = str(event.path)
        self.query_one("#folder-input", Input).value = folder
        recursive = self.query_one("#recursive-switch", Switch).value
        self._refresh_preview(folder, recursive)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "lossless-switch":
            self._sync_lossless_state()
        elif event.switch.id == "recursive-switch" and not self._busy:
            self._refresh_preview(
                self.query_one("#folder-input", Input).value,
                event.value,
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "folder-input" and not self._busy:
            self._open_typed_folder()

    def _open_typed_folder(self) -> None:
        raw = self.query_one("#folder-input", Input).value
        folder, err = check_folder(raw)
        if err or not folder:
            self.query_one("#preview-label", Static).update(f"[red]{err}[/red]")
            return
        self.query_one("#folder-input", Input).value = folder
        tree = self.query_one("#folder-tree", FolderTree)
        tree.path = Path(folder)
        recursive = self.query_one("#recursive-switch", Switch).value
        self._refresh_preview(folder, recursive)

    @work(thread=True, exclusive=True, group="preview")
    def _refresh_preview(self, raw_path: str, recursive: bool = True) -> None:
        folder, err = check_folder(raw_path)
        if err or not folder:
            self.call_from_thread(
                self.query_one("#preview-label", Static).update,
                f"[red]{err}[/red]",
            )
            return
        files = scan_folder(folder, self._settings.supported_formats, recursive=recursive)
        total_bytes = sum(os.path.getsize(f) for f in files if os.path.exists(f))
        msg = f"{len(files)} supported image(s) found ({format_bytes(total_bytes)})"
        self.call_from_thread(self.query_one("#preview-label", Static).update, msg)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "go-btn":
            self._open_typed_folder()
        elif bid == "convert-btn":
            self.action_start_convert()
        elif bid == "dry-run-btn":
            self.action_start_dry_run()
        elif bid == "save-cfg-btn":
            self._save_current_config()
        elif bid == "cancel-btn":
            self.action_cancel_batch()

    def _save_current_config(self) -> None:
        settings = self._read_settings()
        if not settings:
            return
        saved_to = save_config(settings)
        log = self.query_one("#log-view", RichLog)
        if saved_to:
            self._settings = settings
            log.write(f"[green]Settings saved to[/green] {saved_to}")
        else:
            log.write("[red]Failed to save settings.[/red]")

    def action_start_dry_run(self) -> None:
        self._launch(dry_run=True)

    def action_start_convert(self) -> None:
        self._launch(dry_run=False)

    def action_cancel_batch(self) -> None:
        if self._busy and self._cancel_event:
            self._cancel_event.set()
            self.query_one("#status-line", Static).update("Cancelling after active files finish...")

    def action_quit_app(self) -> None:
        if self._busy and self._cancel_event:
            self._cancel_event.set()
        self.exit()

    def _launch(self, dry_run: bool) -> None:
        if self._busy:
            return
        log = self.query_one("#log-view", RichLog)
        settings = self._read_settings()
        if not settings:
            return

        folder, err = check_folder(self.query_one("#folder-input", Input).value)
        if err or not folder:
            log.write(f"[red]{err}[/red]")
            return

        images = scan_folder(folder, settings.supported_formats, recursive=settings.recursive)
        if not images:
            log.write("[yellow]No supported images found in that folder.[/yellow]")
            return

        plan, skipped = plan_conversions(images, overwrite=settings.overwrite)
        if dry_run:
            log.clear()
            log.write(f"[cyan]Dry run:[/cyan] {len(plan)} file(s) to convert, {len(skipped)} skipped.")
            for src, dst in plan:
                log.write(f"  {src} -> {os.path.basename(dst)}")
            return

        if not plan:
            log.write(f"[yellow]Nothing to convert ({len(skipped)} skipped due to name conflicts).[/yellow]")
            return

        if settings.delete_originals:
            def on_confirm(confirmed: Optional[bool]) -> None:
                if confirmed:
                    self._begin_worker(settings, plan, skipped)

            self.push_screen(ConfirmDeleteScreen(len(plan)), on_confirm)
        else:
            self._begin_worker(settings, plan, skipped)

    def _begin_worker(self, settings: Settings, plan: list, skipped: list) -> None:
        log = self.query_one("#log-view", RichLog)
        log.clear()
        mode = "lossless" if settings.lossless else f"quality={settings.quality}"
        log.write(f"[cyan]Starting:[/cyan] {len(plan)} file(s), {mode}, method={settings.method}, workers={settings.max_workers}")
        if skipped:
            log.write(f"[yellow]Skipped {len(skipped)} file(s) due to name conflicts.[/yellow]")

        bar = self.query_one("#progress-bar", ProgressBar)
        bar.update(total=len(plan), progress=0)
        self.query_one("#status-line", Static).update(f"0/{len(plan)}...")
        self._cancel_event = threading.Event()
        self._set_running(True)
        self._run_worker(settings, plan, skipped, self._cancel_event)

    @work(thread=True)
    def _run_worker(self, settings: Settings, plan: list, skipped: list, cancel_event: threading.Event) -> None:
        def on_event(ev: BatchEvent) -> None:
            self.call_from_thread(self._handle_batch_event, ev)

        run_batch(settings, plan, skipped=skipped, on_event=on_event, cancel_event=cancel_event)

    def _handle_batch_event(self, ev: BatchEvent) -> None:
        log = self.query_one("#log-view", RichLog)
        bar = self.query_one("#progress-bar", ProgressBar)
        status = self.query_one("#status-line", Static)

        if ev.kind == "file_completed":
            bar.update(progress=ev.completed)
            pct = (100.0 * (ev.bytes_after - ev.bytes_before) / ev.bytes_before) if ev.bytes_before else 0.0
            status.update(f"{ev.completed}/{ev.total} - {os.path.basename(ev.dst)}")
            log.write(
                f"[green]OK[/green] {os.path.basename(ev.src)} -> {os.path.basename(ev.dst)} "
                f"({format_bytes(ev.bytes_before)} -> {format_bytes(ev.bytes_after)}, {pct:+.1f}%)"
            )
        elif ev.kind == "file_failed":
            bar.update(progress=ev.completed)
            status.update(f"{ev.completed}/{ev.total} - failed: {os.path.basename(ev.src)}")
            log.write(f"[red]FAIL[/red] {ev.src}: {ev.error}")
        elif ev.kind == "batch_finished" and ev.summary is not None:
            s = ev.summary
            self._set_running(False)
            saved = s.saved_bytes
            pct = (100.0 * saved / s.bytes_before) if s.bytes_before else 0.0
            label = "Cancelled" if s.cancelled else "Done"
            direction = "saved" if saved >= 0 else "larger"
            summary_text = (
                f"{label}: {s.succeeded} succeeded, {s.failed} failed, {s.skipped} skipped "
                f"({format_bytes(abs(saved))} {direction}, {abs(pct):.1f}%)"
            )
            status.update(summary_text)
            color = "yellow" if (s.failed or s.skipped or s.cancelled) else "green"
            log.write(f"[{color}]{summary_text}[/{color}]")
            recursive = self.query_one("#recursive-switch", Switch).value
            self._refresh_preview(self.query_one("#folder-input", Input).value, recursive)


def run_tui(initial_folder: Optional[str] = None) -> None:
    """Launch the interactive Textual TUI."""
    # Silence root console logging so core.py logs never corrupt the TUI screen
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    root.addHandler(logging.NullHandler())

    WebPConverterApp(initial_folder=initial_folder).run()
