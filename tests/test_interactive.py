import asyncio

import pytest
from textual.widgets import Input, Static

from easysubs.forms import Form
from easysubs.models import Candidate, Query, Title
from easysubs.net import Network
from easysubs.tui import SubsApp


def result(query, language="en"):
    return Candidate(
        "gestdown",
        "one",
        query.title,
        language,
        "",
        "Matching release",
        season=query.season,
        episode=query.episode,
        score=85,
        reasons=["episode"],
    )


async def wait_for(pilot, condition):
    for _ in range(100):
        await pilot.pause(0.03)
        if condition():
            # A pushed screen exists before its children have completed layout/autofocus.
            await pilot.pause(0.1)
            if condition():
                return
    assert condition()


def test_detected_series_asks_for_season_and_episode(monkeypatch):
    monkeypatch.setattr(
        "easysubs.tui.discover_titles",
        lambda *args: ([Title("Breaking Bad", "tt0903747", "series", 2008, 100)], []),
    )
    queries = []

    def collect(query, *args):
        queries.append((query.season, query.episode))
        return [result(query)], []

    monkeypatch.setattr("easysubs.tui.collect", collect)

    async def scenario():
        app = SubsApp(Query.parse("Breaking Bad", allow_partial=True), ["en"], Network())
        async with app.run_test(size=(100, 32)) as pilot:
            await wait_for(pilot, lambda: isinstance(app.screen, Form))
            assert queries == []  # never search a guessed/default episode
            app.screen.query_one("#season", Input).value = "2"
            app.screen.query_one("#episode", Input).value = "3"
            app.screen.query_one("#episode", Input).focus()
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.mode == "subtitles")
            assert queries == [(2, 3)]
            assert app.media.kind == "series"
            assert app.episode_label() == " · S02E03"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw,season,episode,focus",
    [
        ("Show S03", 3, None, "episode"),
        ("Show E04", None, 4, "season"),
    ],
)
def test_partial_episode_preserves_detected_information(monkeypatch, raw, season, episode, focus):
    monkeypatch.setattr(
        "easysubs.tui.discover_titles", lambda *args: ([Title("Show", "tt1234567", "series", 2020, 100)], [])
    )

    async def scenario():
        query = Query.parse(raw, allow_partial=True)
        assert (query.season, query.episode) == (season, episode)
        app = SubsApp(query, ["en"], Network())
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: isinstance(app.screen, Form))
            assert app.screen.query_one("#season", Input).value == str(season if season is not None else 1)
            assert app.screen.query_one("#episode", Input).value == (str(episode) if episode else "")
            assert app.focused.id == focus
            await pilot.press("escape")
            assert not isinstance(app.screen, Form)
            assert app.is_running

    asyncio.run(scenario())


def test_invalid_episode_stays_in_form_and_specials_work(monkeypatch):
    monkeypatch.setattr("easysubs.tui.collect", lambda query, *args: ([result(query)], []))

    async def scenario():
        query = Query("Show", "Show", imdb="tt1234567", media_type="series")
        app = SubsApp(query, ["en"], Network(), initial=[result(query)])
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("e")
            app.screen.query_one("#season", Input).value = "-1"
            app.screen.query_one("#episode", Input).value = "zero"
            await pilot.click("#continue")
            assert isinstance(app.screen, Form)
            assert "numbers" in str(app.screen.query_one("#form-error", Static).render())
            app.screen.query_one("#season", Input).value = "0"
            app.screen.query_one("#episode", Input).value = "1"
            app.screen.query_one("#episode", Input).focus()
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.mode == "subtitles")
            assert (query.season, query.episode) == (0, 1)

    asyncio.run(scenario())


def test_unrecognized_filename_aliases_then_episode_preserve_file(monkeypatch, tmp_path):
    video = tmp_path / "unrecognizable.mkv"
    video.touch()
    names = []

    def discover(query, net):
        names.append(query.title)
        if query.title == "La casa de papel":
            return [Title("Money Heist", "tt6468322", "series", 2017, 100)], []
        return [], []

    monkeypatch.setattr("easysubs.tui.discover_titles", discover)
    monkeypatch.setattr(
        "easysubs.tui.collect", lambda query, *args: ([result(query)] if query.imdb else [], [])
    )

    async def scenario():
        query = Query.parse(str(video), allow_partial=True)
        app = SubsApp(query, ["en"], Network())
        async with app.run_test(size=(100, 32)) as pilot:
            await wait_for(pilot, lambda: isinstance(app.screen, Form))
            app.screen.query_one("#names", Input).value = "Money Heist | La casa de papel"
            await pilot.press("enter")
            await wait_for(pilot, lambda: isinstance(app.screen, Form) and app.mode == "episode")
            app.screen.query_one("#episode", Input).value = "5"
            await pilot.click("#continue")
            await wait_for(pilot, lambda: app.mode == "subtitles")
            assert query.path == video
            assert query.title == "Money Heist"
            assert (query.season, query.episode) == (1, 5)
            assert "Money Heist" in names and "La casa de papel" in names

    asyncio.run(scenario())


def test_ambiguous_series_choose_version_then_episode(monkeypatch):
    titles = [
        Title("The Office", "tt0386676", "series", 2005, 100),
        Title("The Office", "tt0290978", "series", 2001, 100),
    ]
    monkeypatch.setattr("easysubs.tui.discover_titles", lambda *args: (titles, []))

    async def scenario():
        app = SubsApp(Query.parse("The Office", allow_partial=True), ["en"], Network())
        async with app.run_test(size=(100, 32)) as pilot:
            await wait_for(pilot, lambda: app.mode == "titles")
            await pilot.press("down", "enter")
            await wait_for(pilot, lambda: isinstance(app.screen, Form))
            assert app.media.imdb == "tt0290978"
            assert app.mode == "episode"

    asyncio.run(scenario())


def test_change_language_in_place(monkeypatch):
    seen = []

    def collect(query, languages, *args):
        seen.extend(languages)
        return [result(query, languages[0])], []

    monkeypatch.setattr("easysubs.tui.collect", collect)

    async def scenario():
        query = Query("Arrival", "Arrival", 2016, imdb="tt2543164")
        app = SubsApp(query, ["en"], Network(), initial=[result(query)])
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("l")
            app.screen.query_one("#languages", Input).value = "Hindi"
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.mode == "subtitles")
            assert app.languages == ["hi"]
            assert seen == ["hi"]

    asyncio.run(scenario())


def test_complete_episode_does_not_prompt(monkeypatch):
    monkeypatch.setattr(
        "easysubs.tui.discover_titles", lambda *args: ([Title("Show", "tt1234567", "series", 2020, 100)], [])
    )
    monkeypatch.setattr("easysubs.tui.collect", lambda query, *args: ([result(query)], []))

    async def scenario():
        query = Query.parse("Show S03E02", allow_partial=True)
        app = SubsApp(query, ["en"], Network())
        async with app.run_test(size=(100, 32)) as pilot:
            await wait_for(pilot, lambda: app.mode == "subtitles")
            assert not isinstance(app.screen, Form)
            assert (query.season, query.episode) == (3, 2)

    asyncio.run(scenario())


def test_cancel_title_correction_keeps_results():
    async def scenario():
        query = Query("Arrival", "Arrival", 2016, imdb="tt2543164")
        app = SubsApp(query, ["en"], Network(), initial=[result(query)])
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("t")
            assert isinstance(app.screen, Form)
            await pilot.press("escape")
            assert app.mode == "subtitles"
            assert len(app.candidates) == 1
            assert app.is_running

    asyncio.run(scenario())
