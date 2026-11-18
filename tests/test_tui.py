import asyncio
from PIL import Image
from textual.widgets import Input, ProgressBar, Static, Switch

from webp_converter.tui import ConfirmDeleteScreen, WebPConverterApp


def test_tui_dry_run_and_convert(tmp_path):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "one.png")
    Image.new("RGB", (16, 16), "blue").save(tmp_path / "two.jpg")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.theme == "nord"
            # Lossless switch disables quality input
            await pilot.click("#lossless-switch")
            await pilot.pause(0.1)
            assert app.query_one("#quality-input", Input).disabled is True
            await pilot.click("#lossless-switch")
            await pilot.pause(0.1)

            # Dry run leaves folder untouched
            await pilot.click("#dry-run-btn")
            await pilot.pause()
            assert list(tmp_path.glob("*.webp")) == []

            # Real convert produces 2 WebPs and keeps originals
            await pilot.click("#convert-btn")
            for _ in range(40):
                await pilot.pause(0.1)
                if not app._busy:
                    break
            assert not app._busy
            assert len(list(tmp_path.glob("*.webp"))) == 2
            assert len(list(tmp_path.glob("*.png"))) == 1
            assert len(list(tmp_path.glob("*.jpg"))) == 1
            assert app.query_one("#progress-bar", ProgressBar).progress == 2

    asyncio.run(scenario())


def test_tui_delete_originals_shows_confirmation(tmp_path):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "pic.png")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one("#delete-switch", Switch).value = True
            await pilot.pause()
            await pilot.click("#convert-btn")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDeleteScreen)
            await pilot.click("#cancel-delete")
            await pilot.pause()
            assert (tmp_path / "pic.png").exists()
            assert list(tmp_path.glob("*.webp")) == []

    asyncio.run(scenario())


def test_tui_formats_filter(tmp_path):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "keep.png")
    Image.new("RGB", (16, 16), "blue").save(tmp_path / "skip.jpg")

    async def scenario():
        app = WebPConverterApp(initial_folder=str(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one("#formats-input", Input).value = ".png"
            await pilot.click("#convert-btn")
            for _ in range(40):
                await pilot.pause(0.1)
                if not app._busy:
                    break
            assert (tmp_path / "keep.webp").exists()
            assert not (tmp_path / "skip.webp").exists()

    asyncio.run(scenario())
