import asyncio

from textual.widgets import DataTable, Input, Static

from easysubs.models import Candidate, Query
from easysubs.net import Network
from easysubs.tui import SubsApp


def candidates():
    return [
        Candidate(
            "opensubtitles",
            "1",
            "Arrival",
            "en",
            "https://example.org/1",
            "Arrival.2016.1080p.BluRay-SPARKS",
            score=96,
            reasons=["title", "year", "release group"],
        ),
        Candidate(
            "yify",
            "2",
            "Arrival",
            "en",
            "https://example.org/2",
            "Arrival.2016.720p.WEB-DL",
            score=80,
            reasons=["title", "year"],
        ),
    ]


def test_tui_filter_keyboard_download_and_sidecar(tmp_path, monkeypatch):
    video = tmp_path / "Arrival.2016.1080p.BluRay-SPARKS.mkv"
    video.touch()
    calls = []

    def fake_download(candidate, query, net, output, force):
        calls.append(candidate.id)
        path = video.with_suffix(".en.srt")
        path.write_text("captions")
        return path

    monkeypatch.setattr("easysubs.tui.download", fake_download)

    async def scenario():
        app = SubsApp(Query.parse(str(video)), ["en"], Network(), initial=candidates())
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            assert app.query_one(DataTable).row_count == 2
            await pilot.press("slash")
            assert app.focused is app.query_one(Input)
            await pilot.press("y", "i", "f", "y")
            await pilot.pause()
            assert app.query_one(DataTable).row_count == 1
            await pilot.press("enter", "enter")
            await pilot.pause(1)
        assert app.return_value == video.with_suffix(".en.srt")

    asyncio.run(scenario())
    assert calls == ["2"]


def test_failed_download_keeps_picker_open(monkeypatch):
    def fail(*args):
        raise ValueError("Source returned a web page")

    monkeypatch.setattr("easysubs.tui.download", fail)

    async def scenario():
        app = SubsApp(Query.parse("Arrival 2016"), ["en"], Network(), initial=candidates())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert not app.busy
            assert "web page" in str(app.query_one("#status", Static).render())
            assert app.query_one(DataTable).row_count == 2
            await pilot.press("escape")

    asyncio.run(scenario())
