from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from rapidfuzz.fuzz import WRatio
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Input, Static

from .download import destination, download, url_candidates
from .forms import Form
from .models import Query, language_code
from .net import error_message
from .providers import discover_titles
from .service import apply_title, collect, confident_title


class SubsApp(App[Path | None]):
    """Keyboard-first subtitle browser. Network and downloads stay off the UI thread."""

    TITLE = "subs"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #10151b; color: #d8e2eb; }
    #brand { height: 3; padding: 1 3 0 3; color: #71dfba; text-style: bold; }
    #file { height: auto; max-height: 3; padding: 0 3; color: #edf5fb; text-style: bold; }
    #context { height: 2; padding: 0 3; color: #8192a2; }
    #body { margin: 0 2; height: 1fr; }
    Input { background: #18222b; border: tall #263847; margin: 0 0 1 0; }
    Input:focus { border: tall #71dfba; }
    DataTable { height: 1fr; background: #10151b; border: none; }
    DataTable > .datatable--header { background: #1c2933; color: #91a6b7; text-style: bold; }
    DataTable > .datatable--cursor { background: #204b43; color: #e2fff6; text-style: bold; }
    DataTable > .datatable--hover { background: #192f32; }
    DataTable > .datatable--odd-row { background: #141d25; }
    #details { height: 4; margin-top: 1; padding: 0 1; border-left: thick #71dfba; color: #a1b3c2; }
    #status { height: auto; max-height: 3; padding: 1 3 0 3; color: #71dfba; }
    Footer { background: #1c2933; color: #a1b3c2; margin-top: 1; }
    Footer > .footer--key { background: #25453e; color: #a5f5d9; }
    """
    BINDINGS = [
        Binding("slash", "filter", "Filter"),
        Binding("d", "download", "Download"),
        Binding("t", "edit_title", "Title"),
        Binding("e", "edit_episode", "Episode"),
        Binding("l", "edit_language", "Language"),
        Binding("a", "auto_captions", "Auto captions", show=False),
        Binding("escape", "back", "Back / quit", priority=True),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        query,
        languages,
        net,
        *,
        providers=None,
        output=None,
        force=False,
        auto_subs=False,
        initial=None,
    ):
        super().__init__()
        self.media = query
        self.languages = languages
        self.net = net
        self.providers = providers
        self.output = output
        self.force = force
        self.auto_subs = auto_subs
        self.candidates = []
        self.titles = []
        self.visible_indices = []
        self.mode = "loading"
        self.busy = False
        self.warnings = []
        self.aliases = []
        self.initial = initial  # deterministic previews and UI tests

    def compose(self) -> ComposeResult:
        yield Static("s u b s   /   find the words", id="brand")
        yield Static(Text(self.media.path.name if self.media.path else self.media.raw), id="file")
        yield Static(" · ".join(self.languages) + "   /   searching your subtitle sources", id="context")
        with Vertical(id="body"):
            yield Input(placeholder="/ to filter releases, sources, languages…", id="filter")
            yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
            yield Static("Finding the right title…", id="details")
        yield Static("Searching…", id="status")
        yield Footer()

    def on_mount(self):
        self.query_one(DataTable).focus()
        if self.initial is not None:
            self.show_candidates(self.initial, [])
        else:
            self.find_title()

    def ui(self, callback, *args):
        if not self.is_running:
            return
        try:
            return self.call_from_thread(callback, *args)
        except RuntimeError:
            if self.is_running:
                raise

    def status(self, message):
        self.query_one("#status", Static).update(Text(message))

    @work(thread=True, exclusive=True, group="search")
    def find_title(self):
        try:
            if self.media.raw.startswith(("http://", "https://")):
                self.media, candidates = url_candidates(
                    self.media.raw, self.languages, self.auto_subs, self.net.timeout
                )
                self.ui(self.show_candidates, candidates, [])
                return
            unique = {}
            for name in self.aliases or [self.media.title]:
                self.ui(self.status, f"Looking up {name}…")
                lookup = replace(self.media, title=name)
                titles, warnings = discover_titles(lookup, self.net)
                self.warnings.extend(warnings)
                for title in titles:
                    if title.imdb not in unique or title.score > unique[title.imdb].score:
                        unique[title.imdb] = title
            titles = sorted(unique.values(), key=lambda title: -title.score)
            chosen = confident_title(titles, self.media)
            if chosen:
                apply_title(self.media, chosen, allow_incomplete=True)
            elif titles:
                self.ui(self.show_titles, titles)
                return
            if self.needs_episode():
                self.ui(self.ask_episode)
                return
            self.fetch_candidates()
        except Exception as exc:
            self.ui(self.failed, error_message(exc))

    def fetch_candidates(self):
        def progress(name, count, error):
            message = f"{name}: {error}" if error else f"{name}: {count} subtitles found"
            self.ui(self.status, message)

        candidates, warnings = collect(self.media, self.languages, self.net, self.providers, progress)
        self.ui(self.show_candidates, candidates, warnings)

    @work(thread=True, exclusive=True, group="search")
    def load_selected(self):
        try:
            self.fetch_candidates()
        except Exception as exc:
            self.ui(self.failed, error_message(exc))

    def failed(self, message):
        self.mode = "error"
        self.status(message)
        self.query_one("#details", Static).update(
            "Press T to try another title, L for language, or Esc to quit."
        )

    def show_titles(self, titles):
        self.mode, self.titles = "titles", titles
        self.query_one("#context", Static).update("Choose the movie or show you meant")
        self.query_one(Input).placeholder = "Filter titles…"
        self.populate("")
        self.status("A few close matches — Enter to choose the right title.")

    def show_candidates(self, candidates, warnings):
        self.mode, self.candidates = "subtitles", candidates
        self.warnings.extend(warnings)
        self.query_one(Input).value = ""
        self.query_one(Input).placeholder = "Filter releases, sources, languages…"
        self.query_one("#context", Static).update(
            Text(
                f"{self.media.title}{self.episode_label()}  ·  {' / '.join(self.languages)}  ·  {len(candidates)} subtitles"
            )
        )
        self.populate("")
        if candidates:
            message = "Best matches first. Enter to download beside your video."
            if self.warnings:
                message += "  ·  " + "; ".join(self.warnings)
            self.status(message)
        else:
            self.status("No matches yet. T: try another title · E: change episode · L: change language.")
            self.query_one("#details", Static).update(
                Text("; ".join(self.warnings) or "Sources returned no matches.")
            )
            if self.media.raw.startswith(("http://", "https://")):
                self.status("No captions for this language. L: change language · A: include auto captions.")
            else:
                self.ask_title(no_results=True)

    def populate(self, needle):
        table = self.query_one(DataTable)
        table.clear(columns=True)
        items = self.titles if self.mode == "titles" else self.candidates
        scored = [(WRatio(needle.casefold(), item.label.casefold()), i) for i, item in enumerate(items)]
        if needle:
            scored = sorted((pair for pair in scored if pair[0] >= 48), reverse=True)
        self.visible_indices = [i for _, i in scored]
        if self.mode == "titles":
            table.add_columns("MATCH", "TITLE", "YEAR", "TYPE")
            for i in self.visible_indices:
                t = self.titles[i]
                table.add_row(f"{t.score:.0f}", Text(t.name), str(t.year or "—"), t.kind, key=str(i))
        else:
            table.add_columns("MATCH", "LANG", "SOURCE", "RELEASE")
            for i in self.visible_indices:
                c = self.candidates[i]
                label = Text(f"{c.score:.0f}", style="#71dfba" if c.score >= 75 else "#cbd7e0")
                release = (c.release or c.title) + ("  [HI]" if c.hearing_impaired else "")
                table.add_row(label, c.language, c.provider, Text(release), key=str(i))
        if self.visible_indices:
            table.move_cursor(row=0)
            self.show_details(self.visible_indices[0])
        else:
            self.query_one("#details", Static).update("No matches for this filter.")

    @on(Input.Changed, "#filter")
    def filter_changed(self, event):
        if self.mode in {"titles", "subtitles"}:
            self.populate(event.value)

    @on(Input.Submitted, "#filter")
    def filter_submitted(self):
        self.query_one(DataTable).focus()

    @on(DataTable.RowHighlighted)
    def highlighted(self, event):
        if event.row_key.value is not None:
            self.show_details(int(event.row_key.value))

    def show_details(self, index):
        if self.mode == "titles" and index < len(self.titles):
            text = self.titles[index].label + "\nEnter to search subtitles for this title."
        elif self.mode == "subtitles" and index < len(self.candidates):
            c = self.candidates[index]
            target = destination(self.media, c.language, self.output)
            text = f"{c.release or c.title}\nMatched: {', '.join(c.reasons)}\nSave → {target}"
        else:
            return
        self.query_one("#details", Static).update(Text(text))

    @on(DataTable.RowSelected)
    def selected(self, event):
        self.activate(int(event.row_key.value))

    def activate(self, index):
        if self.busy:
            return
        if self.mode == "titles":
            apply_title(self.media, self.titles[index], allow_incomplete=True)
            if self.needs_episode():
                self.ask_episode()
            else:
                self.restart_subtitles()
        elif self.mode == "subtitles":
            self.busy = True
            self.status("Downloading and checking subtitles…")
            self.save_selected(self.candidates[index])

    @work(thread=True, group="download", exclusive=True)
    def save_selected(self, candidate):
        try:
            path = download(candidate, self.media, self.net, self.output, self.force)
            self.ui(self.saved, path)
        except Exception as exc:
            self.ui(self.download_failed, error_message(exc))

    def saved(self, path):
        self.status(f"✓ Saved {path.name}")
        self.set_timer(0.7, lambda: self.exit(path))

    def download_failed(self, message):
        self.busy = False
        self.status(message + "  ·  Choose another release or press Esc.")

    def action_filter(self):
        self.query_one(Input).focus()

    def action_download(self):
        if self.focused is self.query_one(Input):
            return
        row = self.query_one(DataTable).cursor_row
        if 0 <= row < len(self.visible_indices):
            self.activate(self.visible_indices[row])

    def action_back(self):
        if isinstance(self.screen, Form):
            self.screen.dismiss(None)
            return
        if self.focused is self.query_one(Input):
            self.query_one(Input).value = ""
            self.query_one(DataTable).focus()
        elif not self.busy:
            self.exit(None)

    def action_quit(self):
        if isinstance(self.screen, Form):
            return
        if not self.busy:
            self.exit(None)

    def episode_label(self):
        if self.media.season is not None and self.media.episode is not None:
            return f" · S{self.media.season:02}E{self.media.episode:02}"
        return ""

    def needs_episode(self):
        return self.media.kind == "series" and (self.media.season is None or self.media.episode is None)

    def can_edit(self):
        return not self.busy and self.mode != "loading" and not isinstance(self.screen, Form)

    def restart_subtitles(self):
        self.mode = "loading"
        self.status("Searching subtitle sources…")
        self.query_one(DataTable).clear()
        self.load_selected()

    def ask_episode(self):
        self.episode_return_mode = self.mode
        self.mode = "episode"
        self.status("Choose the season and episode.")

        def validate(values):
            try:
                season, episode = int(values["season"]), int(values["episode"])
            except ValueError:
                raise ValueError("Enter numbers, such as season 1 and episode 3.") from None
            if season < 0 or episode < 1:
                raise ValueError("Season must be 0 or higher (0 = specials); episode must be 1 or higher.")

        fields = [
            ("season", "Season", str(self.media.season if self.media.season is not None else 1), "1"),
            ("episode", "Episode", str(self.media.episode) if self.media.episode is not None else "", "1"),
        ]
        self.push_screen(
            Form(
                self.media.title,
                "Which episode would you like subtitles for?",
                fields,
                validate=validate,
                focus="episode" if self.media.season is not None else "season",
            ),
            self.episode_chosen,
        )

    def episode_chosen(self, values):
        if values is None:
            self.mode = (
                self.episode_return_mode if self.episode_return_mode in {"subtitles", "titles"} else "idle"
            )
            self.status("Episode selection cancelled. E: choose episode · T: change title.")
            return
        self.media.season = int(values["season"])
        self.media.episode = int(values["episode"])
        self.media.media_type = "series"
        self.warnings = []
        if self.media.imdb:
            self.restart_subtitles()
        else:
            self.mode = "loading"
            self.find_title()

    def action_edit_episode(self):
        if self.can_edit() and not self.media.raw.startswith(("http://", "https://")):
            if self.media.imdb and self.media.kind == "movie":
                self.status("This title is a movie. Press T to choose a TV show.")
                return
            self.ask_episode()

    def ask_title(self, no_results=False):
        if isinstance(self.screen, Form):
            return
        self.title_return_mode = self.mode
        self.mode = "title-entry"

        def validate(values):
            names = [name.strip() for name in values["names"].split("|") if name.strip()]
            if not names:
                raise ValueError("Enter at least one movie or show name.")
            if len(names) > 5:
                raise ValueError("Try up to five names at a time.")
            for name in names:
                Query.parse(name, allow_partial=True)

        hint = "Try the official title, a translated title, or a different spelling."
        hint += " Separate alternatives with |. You can include a year or S01E03."
        self.push_screen(
            Form(
                "Let's find your video" if no_results else "Change title",
                hint,
                [
                    (
                        "names",
                        "Movie / show name(s)",
                        "" if no_results else self.media.title,
                        "Money Heist | La casa de papel",
                    )
                ],
                validate=validate,
            ),
            self.title_entered,
        )

    def title_entered(self, values):
        if values is None:
            self.mode = (
                self.title_return_mode if self.title_return_mode in {"subtitles", "titles"} else "idle"
            )
            self.status("T: enter a title · E: choose episode · L: change language · Esc: quit.")
            return
        names = [name.strip() for name in values["names"].split("|") if name.strip()]
        parsed = [Query.parse(name, allow_partial=True) for name in names]
        first = parsed[0]
        self.aliases = [query.title for query in parsed]
        self.media.title = first.title
        self.media.imdb = first.imdb
        self.media.year = first.year
        if first.season is not None or first.episode is not None:
            self.media.season, self.media.episode = first.season, first.episode
            self.media.media_type = "series"
        self.warnings = []
        self.mode = "loading"
        self.query_one(DataTable).clear()
        self.status("Looking up your titles…")
        self.find_title()

    def action_edit_title(self):
        if self.can_edit() and not self.media.raw.startswith(("http://", "https://")):
            self.ask_title()

    def action_edit_language(self):
        if not self.can_edit():
            return

        def validate(values):
            for value in values["languages"].split(","):
                language_code(value)

        self.push_screen(
            Form(
                "Subtitle language",
                "Use a name or code. Separate languages with commas.",
                [("languages", "Language(s)", ",".join(self.languages), "en,hi")],
                validate=validate,
            ),
            self.language_chosen,
        )

    def language_chosen(self, values):
        if values is None:
            return
        self.languages = list(dict.fromkeys(language_code(value) for value in values["languages"].split(",")))
        self.warnings = []
        if self.needs_episode():
            self.ask_episode()
        elif self.media.raw.startswith(("http://", "https://")) or not self.media.imdb:
            self.mode = "loading"
            self.find_title()
        else:
            self.restart_subtitles()

    def action_auto_captions(self):
        if self.can_edit() and self.media.raw.startswith(("http://", "https://")):
            self.auto_subs = not self.auto_subs
            self.mode = "loading"
            self.status("Checking automatic captions…")
            self.find_title()
