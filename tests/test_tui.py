import asyncio
from PIL import Image
from textual.widgets import Button, DataTable, Input, ProgressBar, Select, SelectionList, Switch, TabbedContent

from webp_converter.tui import ConfirmDeleteScreen, WebPConverterApp


async def _wait_for_scan(app: WebPConverterApp, pilot, expected_min: int = 1) -> None:
    for _ in range(40):
        await pilot.pause(0.05)
        if len(app._items) >= expected_min:
            return


async def _wait_for_batch(app: WebPConverterApp, pilot) -> None:
    for _ in range(60):
        await pilot.pause(0.05)
        if not app._busy:
            return


def test_tui_nord_queue_presets_dry_run_and_convert(tmp_path):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "one.png")
    Image.new("RGB", (16, 16), "blue").save(tmp_path / "two.jpg")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_scan(app, pilot, expected_min=2)
            assert app.theme == "nord"
            table = app.query_one("#queue-table", DataTable)
            assert table.row_count == 2

            # Preset switching updates settings & disables quality for Lossless
            preset = app.query_one("#preset-select", Select)
            preset.value = "lossless"
            await pilot.pause(0.1)
            assert app.query_one("#quality-input", Input).disabled is True
            assert app.query_one("#preset-btn-lossless", Button).variant == "primary"

            # Clicking a 1-click preset button in Settings updates inputs & dropdown
            app.query_one("#main-tabs", TabbedContent).active = "tab-settings"
            await pilot.pause(0.05)
            await pilot.click("#preset-btn-balanced")
            await pilot.pause(0.1)
            assert app.query_one("#quality-input", Input).value == "82"
            assert app.query_one("#method-input", Input).value == "4"
            assert preset.value == "balanced"
            assert app.query_one("#preset-btn-balanced", Button).variant == "primary"

            await pilot.click("#preset-btn-high")
            await pilot.pause(0.1)
            assert app.query_one("#quality-input", Input).disabled is False
            assert app.query_one("#quality-input", Input).value == "90"
            app.query_one("#main-tabs", TabbedContent).active = "tab-queue"
            await pilot.pause(0.05)

            # Sidebar toggle
            app.action_toggle_sidebar()
            await pilot.pause(0.05)
            assert app.query_one("#left-pane").has_class("hidden")
            app.action_toggle_sidebar()
            await pilot.pause(0.05)
            assert not app.query_one("#left-pane").has_class("hidden")

            # Dry run leaves folder untouched and opens Log tab
            await pilot.click("#dry-run-btn")
            await pilot.pause(0.1)
            assert list(tmp_path.glob("*.webp")) == []
            assert app.query_one("#main-tabs", TabbedContent).active == "tab-log"

            # Real convert produces 2 WebPs, keeps originals, updates statuses
            await pilot.click("#convert-btn")
            await _wait_for_batch(app, pilot)
            assert not app._busy
            assert len(list(tmp_path.glob("*.webp"))) == 2
            assert len(list(tmp_path.glob("*.png"))) == 1
            assert len(list(tmp_path.glob("*.jpg"))) == 1
            assert all(i.status == "done" for i in app._items.values())
            assert app.query_one("#progress-bar", ProgressBar).progress == 2

    asyncio.run(scenario())


def test_tui_delete_originals_shows_confirmation(tmp_path):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "pic.png")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_scan(app, pilot, expected_min=1)
            app.query_one("#delete-switch", Switch).value = True
            await pilot.pause(0.1)
            await pilot.click("#convert-btn")
            await pilot.pause(0.1)
            assert isinstance(app.screen, ConfirmDeleteScreen)
            await pilot.click("#cancel-delete")
            await pilot.pause(0.1)
            assert (tmp_path / "pic.png").exists()
            assert list(tmp_path.glob("*.webp")) == []

    asyncio.run(scenario())


def test_tui_formats_selection_list_and_filter(tmp_path):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "keep.png")
    Image.new("RGB", (16, 16), "blue").save(tmp_path / "skip.jpg")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_scan(app, pilot, expected_min=2)
            table = app.query_one("#queue-table", DataTable)

            # Live filter narrows visible table rows
            flt = app.query_one("#filter-input", Input)
            flt.value = "keep"
            await pilot.pause(0.1)
            assert table.row_count == 1
            flt.value = ""
            await pilot.pause(0.1)
            assert table.row_count == 2

            # Deselecting .jpg leaves only .png in the queue
            fmt_list = app.query_one("#formats-list", SelectionList)
            fmt_list.deselect(".jpg")
            for _ in range(30):
                await pilot.pause(0.05)
                if len(app._items) == 1:
                    break
            assert len(app._items) == 1

            await pilot.click("#convert-btn")
            await _wait_for_batch(app, pilot)
            assert (tmp_path / "keep.webp").exists()
            assert not (tmp_path / "skip.webp").exists()

    asyncio.run(scenario())


def test_tui_failed_file_enables_retry(tmp_path):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "good.png")
    (tmp_path / "bad.png").write_bytes(b"not a valid png")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_scan(app, pilot, expected_min=2)
            await pilot.click("#convert-btn")
            await _wait_for_batch(app, pilot)
            assert (tmp_path / "good.webp").exists()
            assert not (tmp_path / "bad.webp").exists()
            assert app.query_one("#retry-btn", Button).disabled is False

    asyncio.run(scenario())


def test_tui_only_scans_selected_folder_and_ignores_stale_scans(tmp_path):
    sub = tmp_path / "nested"
    sub.mkdir()
    Image.new("RGB", (16, 16), "red").save(tmp_path / "direct.png")
    Image.new("RGB", (16, 16), "blue").save(sub / "outside.gif")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_scan(app, pilot, expected_min=1)
            await pilot.pause(0.1)
            # Only direct.png is queued; nested/outside.gif is NOT added
            assert [i.rel_path for i in app._items.values()] == ["direct.png"]

            # A stale scan from an earlier scan_id must NOT overwrite the current queue
            stale_id = app._scan_id - 1
            app._apply_scan_results(stale_id, str(sub), [])
            assert [i.rel_path for i in app._items.values()] == ["direct.png"]

            # Explicitly enabling Include subfolders adds nested/outside.gif
            app.query_one("#recursive-switch", Switch).value = True
            await _wait_for_scan(app, pilot, expected_min=2)
            assert len(app._items) == 2

    asyncio.run(scenario())


def test_tui_filter_escape_and_enter_leave_input(tmp_path):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "alpha.png")
    Image.new("RGB", (16, 16), "blue").save(tmp_path / "beta.png")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_scan(app, pilot, expected_min=2)
            flt = app.query_one("#filter-input", Input)
            table = app.query_one("#queue-table", DataTable)

            # Focus filter via '/'
            await pilot.press("slash")
            await pilot.pause(0.05)
            assert flt.has_focus

            # Typing + Enter keeps filter and moves focus to queue table
            await pilot.press("a", "l", "p", "h", "a", "enter")
            await pilot.pause(0.05)
            assert not flt.has_focus
            assert table.has_focus
            assert table.row_count == 1

            # Focus filter again and press Escape -> clears filter AND leaves input
            await pilot.press("slash")
            await pilot.pause(0.05)
            assert flt.has_focus
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert not flt.has_focus
            assert table.has_focus
            assert flt.value == ""
            assert table.row_count == 2

            # Single-key shortcuts work again immediately after Escape
            await pilot.press("2")
            await pilot.pause(0.05)
            assert app.query_one("#main-tabs", TabbedContent).active == "tab-settings"

    asyncio.run(scenario())


def test_tui_lossless_preserves_quality_and_allows_empty_quality(tmp_path):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "q.png")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_scan(app, pilot, expected_min=1)
            q_input = app.query_one("#quality-input", Input)
            l_switch = app.query_one("#lossless-switch", Switch)

            # Custom quality 85 must survive toggling Lossless ON then OFF
            q_input.value = "85"
            await pilot.pause(0.1)
            l_switch.value = True
            await pilot.pause(0.1)
            assert q_input.value == "85"
            l_switch.value = False
            await pilot.pause(0.1)
            assert q_input.value == "85"

            # Empty quality + Lossless ON must NOT block conversion
            q_input.value = ""
            await pilot.pause(0.05)
            l_switch.value = True
            await pilot.pause(0.1)
            await pilot.click("#convert-btn")
            await _wait_for_batch(app, pilot)
            assert (tmp_path / "q.webp").exists()

    asyncio.run(scenario())


def test_tui_confirm_delete_dismisses_with_escape(tmp_path):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "pic.png")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_scan(app, pilot, expected_min=1)
            app.query_one("#delete-switch", Switch).value = True
            await pilot.pause(0.1)
            await pilot.click("#convert-btn")
            await pilot.pause(0.1)
            assert isinstance(app.screen, ConfirmDeleteScreen)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ConfirmDeleteScreen)
            assert (tmp_path / "pic.png").exists()

    asyncio.run(scenario())


def test_tui_rerun_respects_overwrite_false_and_deleted_originals(tmp_path):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "pic.png")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_scan(app, pilot, expected_min=1)
            # First run creates pic.webp
            await pilot.click("#convert-btn")
            await _wait_for_batch(app, pilot)
            assert (tmp_path / "pic.webp").exists()
            original_bytes = (tmp_path / "pic.webp").read_bytes()

            # Second run with overwrite=False must NOT overwrite pic.webp; creates pic_png.webp
            await pilot.click("#convert-btn")
            await _wait_for_batch(app, pilot)
            assert (tmp_path / "pic.webp").read_bytes() == original_bytes
            assert (tmp_path / "pic_png.webp").exists()

            # Delete original manually (simulating delete_originals=True) then re-click Convert -> no crash/failure
            (tmp_path / "pic.png").unlink()
            await pilot.click("#convert-btn")
            await pilot.pause(0.1)
            assert app.query_one("#retry-btn", Button).disabled is True

    asyncio.run(scenario())


def test_tui_markup_paths_escape_in_settings_and_narrow_80x24(tmp_path):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "img[bold].png")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await _wait_for_scan(app, pilot, expected_min=1)
            # All 4 action buttons must fit inside the 80-col viewport
            actions_right = app.query_one("#actions-row").region.right
            for bid in ("#convert-btn", "#dry-run-btn", "#retry-btn", "#cancel-btn"):
                assert app.query_one(bid).region.right <= actions_right

            # Converting a filename with Rich markup brackets must not raise MarkupError
            await pilot.click("#convert-btn")
            await _wait_for_batch(app, pilot)
            assert (tmp_path / "img[bold].webp").exists()

            # Pressing Esc inside a Settings input must stay on Settings tab
            app.query_one("#main-tabs", TabbedContent).active = "tab-settings"
            await pilot.pause(0.05)
            m_input = app.query_one("#method-input", Input)
            m_input.focus()
            await pilot.pause(0.05)
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert app.query_one("#main-tabs", TabbedContent).active == "tab-settings"
            assert not m_input.has_focus

    asyncio.run(scenario())


def test_tui_folder_switch_clears_filter_and_shows_empty_hint(tmp_path):
    folder_a = tmp_path / "A"
    folder_b = tmp_path / "B"
    sub_b = folder_b / "Nested"
    folder_a.mkdir()
    sub_b.mkdir(parents=True)
    Image.new("RGB", (16, 16), "red").save(folder_a / "photo.jpg")
    Image.new("RGB", (16, 16), "blue").save(sub_b / "inside.png")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(folder_b))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.3)
            # folder_b has no direct images, only 1 subfolder -> hint mentions subfolder
            assert "1 subfolder(s)" in app._empty_hint

            # Set a non-matching filter, then open folder_a -> filter clears & photo.jpg shows
            app.query_one("#filter-input", Input).value = "nomatch"
            app.query_one("#folder-input", Input).value = str(folder_a)
            await pilot.click("#go-btn")
            await _wait_for_scan(app, pilot, expected_min=1)
            assert app.query_one("#filter-input", Input).value == ""
            assert app.query_one("#queue-table", DataTable).row_count == 1

    asyncio.run(scenario())


def test_tui_folder_tree_icon_click_selects_folder(tmp_path):
    from webp_converter.tui import FolderTree

    sub = tmp_path / "LeafWithImages"
    sub.mkdir()
    Image.new("RGB", (16, 16), "red").save(sub / "test1.png")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.2)
            tree = app.query_one("#folder-tree", FolderTree)
            tree.path = tmp_path
            await pilot.pause(0.2)
            # Click directly on the folder icon/toggle region (x=4, row 1)
            await pilot.click("#folder-tree", offset=(4, 1))
            await _wait_for_scan(app, pilot, expected_min=1)
            assert app._active_folder == str(sub)
            assert app.query_one("#queue-table", DataTable).row_count == 1

    asyncio.run(scenario())
