from __future__ import annotations

from .download import embedded
from .models import Query, Title, rank_candidates
from .providers import discover_titles, search


def confident_title(titles: list[Title], query: Query) -> Title | None:
    if not titles:
        return None
    top = titles[0]
    if query.imdb == top.imdb:
        return top
    if top.score >= 82 and (len(titles) == 1 or top.score - titles[1].score >= 5):
        return top
    return None


def apply_title(query: Query, title: Title, *, allow_incomplete: bool = False) -> Query:
    if title.kind == "series" and (query.episode is None or query.season is None) and not allow_incomplete:
        raise ValueError("This is a TV show. Add an episode, for example S01E01 or --season 1 --episode 1.")
    query.title, query.imdb, query.media_type = title.name, title.imdb, title.kind
    if title.kind == "movie":
        query.season = query.episode = None
    if title.kind == "movie":
        query.year = query.year or title.year
    return query


def resolve(query, net):
    titles, warnings = discover_titles(query, net)
    chosen = confident_title(titles, query)
    if chosen:
        apply_title(query, chosen)
    elif titles:
        suggestions = "; ".join(f"{t.name} ({t.year}) --imdb {t.imdb}" for t in titles[:5])
        raise ValueError(f"Title is ambiguous. Run in a terminal to choose, or specify: {suggestions}")
    elif query.imdb:
        # Explicit IDs remain usable even when the title catalogue is unavailable.
        warnings.append("Could not resolve IMDb title; using the supplied ID.")
    return query, warnings


def collect(query, languages, net, providers=None, progress=None):
    local = embedded(query, languages)
    found, warnings = search(query, languages, net, providers, progress)
    return rank_candidates(local + found, query), warnings
