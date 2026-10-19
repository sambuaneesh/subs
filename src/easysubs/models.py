from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from babelfish import Language
from babelfish.exceptions import LanguageReverseError
from guessit import guessit
from rapidfuzz.fuzz import ratio, token_sort_ratio
from rapidfuzz.utils import default_process

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".ts", ".m2ts", ".wmv"}
SUB_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".ttml"}


@lru_cache(maxsize=256)
def language_code(value: str) -> str:
    value = value.strip().lower().replace("_", "-")
    aliases = {"pob": "pt-BR", "pb": "pt-BR", "br": "pt-BR", "chi": "zh", "gre": "el"}
    value = aliases.get(value, value)
    for parse in (Language.fromietf, Language.fromalpha3b, Language.fromname):
        try:
            return str(parse(value))
        except (ValueError, KeyError, LanguageReverseError):
            pass
    raise ValueError(f"Unknown language: {value!r}. Use en, hi, ja, pt-BR, or a language name.")


def language_matches(actual: str, requested: str) -> bool:
    try:
        actual = language_code(actual)
        requested = language_code(requested)
        return actual == requested or ("-" not in requested and actual.split("-")[0] == requested)
    except ValueError:
        return False


def similarity(a: str, b: str) -> float:
    a, b = default_process(a) or "", default_process(b) or ""
    return max(ratio(a, b), token_sort_ratio(a, b))


@dataclass
class Query:
    raw: str
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    imdb: str | None = None
    path: Path | None = None
    release: dict = field(default_factory=dict)
    media_type: str | None = None

    @property
    def kind(self) -> str:
        return self.media_type or (
            "series" if self.season is not None or self.episode is not None else "movie"
        )

    @classmethod
    def parse(cls, raw: str, *, allow_partial: bool = False, **overrides) -> Query:
        path = Path(raw).expanduser()
        exists = path.is_file()
        name = path.name if exists else raw
        # Natural episode notation is normalized before filename parsing.
        name = re.sub(
            r"\bseason\s*(\d+)\s*(?:episode|ep)\s*(\d+)\b",
            lambda m: f"S{int(m[1]):02}E{int(m[2]):02}",
            name,
            flags=re.I,
        )
        guessed = dict(guessit(name))
        episode = guessed.get("episode")
        season = guessed.get("season")
        if isinstance(episode, list) or isinstance(season, list):
            raise ValueError("Use a single episode per query, or pass a folder for batch downloads.")
        q = cls(
            raw,
            guessed.get("title", name),
            guessed.get("year"),
            season,
            episode,
            path=path.resolve() if exists else None,
            release=guessed,
        )
        if re.fullmatch(r"tt\d{5,12}", raw):
            q.imdb = raw
        for key, value in overrides.items():
            if value is not None:
                setattr(q, key, value)
        if q.episode is not None and q.season is None and not allow_partial:
            q.season = 1
        if q.season is not None and q.episode is None and not allow_partial:
            raise ValueError(
                "Add an episode, for example 'Breaking Bad S01E01'. For a season, pass its folder."
            )
        return q


@dataclass
class Title:
    name: str
    imdb: str
    kind: str
    year: int | None = None
    score: float = 0

    @property
    def label(self) -> str:
        return f"{self.name} ({self.year or '?'}) · {self.kind} · {self.imdb}"


@dataclass
class Candidate:
    provider: str
    id: str
    title: str
    language: str
    url: str
    release: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    hearing_impaired: bool = False
    downloads: int = 0
    score: float = 0
    reasons: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        hi = " · HI" if self.hearing_impaired else ""
        return f"{self.score:5.1f} · {self.language} · {self.provider}{hi} · {self.release or self.title}"


def rank_candidates(candidates: list[Candidate], query: Query) -> list[Candidate]:
    ranked, seen = [], set()
    for c in candidates:
        key = (c.provider, c.id, c.language)
        if key in seen:
            continue
        seen.add(key)
        if query.year and c.year and query.year != c.year:
            continue
        guessed = dict(guessit(c.release)) if c.release else {}
        season = c.season if c.season is not None else guessed.get("season")
        episode = c.episode if c.episode is not None else guessed.get("episode")
        # A known mismatch is never rescued by fuzzy release-name similarity.
        if query.episode is not None:
            if season is not None and season != query.season:
                continue
            if episode is not None and episode != query.episode:
                continue
        title_score = similarity(query.title, c.title)
        if title_score < 55:
            continue
        c.score = title_score * 0.65
        c.reasons = ["title"]
        if query.year and c.year == query.year:
            c.score += 10
            c.reasons.append("year")
        if query.episode is not None and episode == query.episode and season == query.season:
            c.score += 10
            c.reasons.append("episode")
        for key, weight in (("release_group", 12), ("source", 5), ("screen_size", 4), ("video_codec", 2)):
            if query.release.get(key) and guessed.get(key) == query.release[key]:
                c.score += weight
                c.reasons.append(key.replace("_", " "))
        if not query.release.get("source") and guessed.get("source") in {"Camera", "HD Camera", "Telesync"}:
            c.score -= 12
        if c.provider == "embedded":
            c.score = 100
            c.reasons = ["embedded in this file"]
        c.score = round(min(c.score, 100), 1)
        ranked.append(c)
    return sorted(ranked, key=lambda c: (-c.score, -c.downloads, c.hearing_impaired, c.provider, c.id))
