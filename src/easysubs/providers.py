from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin

from babelfish import Language
from bs4 import BeautifulSoup

from .models import Candidate, Query, Title, language_code, language_matches, rank_candidates, similarity
from .net import Network, error_message

SOURCES = {
    "opensubtitles": "Movies + TV via Stremio's public OpenSubtitles addon; no key",
    "gestdown": "TV subtitles via Gestdown / Addic7ed; no key",
    "yify": "Movie subtitles scraped from YIFY; no key",
    "subdl": "Movies + TV; optional SUBDL_API_KEY",
}


def discover_titles(query: Query, net: Network) -> tuple[list[Title], list[str]]:
    def imdb_search():
        data = net.json(
            f"https://v3.sg.media-imdb.com/suggestion/x/{quote(query.title, safe='')}.json", ttl=86400
        )
        titles = []
        for item in data.get("d", []):
            if not str(item.get("id", "")).startswith("tt"):
                continue
            kind = "series" if item.get("qid") in {"tvSeries", "tvMiniSeries"} else "movie"
            titles.append(Title(item["l"], item["id"], kind, item.get("y")))
        return titles

    def cinemeta():
        if query.imdb:
            data = net.json(f"https://v3-cinemeta.strem.io/meta/{query.kind}/{query.imdb}.json", ttl=86400)
            items = [data["meta"]] if data.get("meta") else []
        else:
            data = net.json(
                f"https://v3-cinemeta.strem.io/catalog/{query.kind}/top/search={quote(query.title, safe='')}.json",
                ttl=86400,
            )
            items = data.get("metas", [])
        result = []
        for item in items:
            year = re.match(r"\d{4}", str(item.get("releaseInfo", "")))
            result.append(
                Title(item["name"], item["id"], item.get("type", query.kind), int(year[0]) if year else None)
            )
        return result

    found, errors = [], []
    functions = [cinemeta] if query.imdb else [imdb_search, cinemeta]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(fn): fn.__name__ for fn in functions}
        for future in as_completed(futures):
            try:
                found.extend(future.result())
            except Exception as exc:
                errors.append(f"{futures[future]}: {error_message(exc)}")
    unique = {}
    for title in found:
        if query.kind == "series" and title.kind != "series":
            continue
        if query.year and title.year and title.year != query.year:
            continue
        title.score = 100 if query.imdb == title.imdb else similarity(query.title, title.name)
        if query.year and title.year is None and not query.imdb:
            title.score -= 7
        if title.score >= 55:
            unique.setdefault(title.imdb, title)
    return sorted(unique.values(), key=lambda t: -t.score), errors


def opensubtitles(query: Query, languages: list[str], net: Network) -> list[Candidate]:
    if not query.imdb:
        return []
    identity = query.imdb
    if query.episode is not None:
        identity += f":{query.season}:{query.episode}"
    data = net.json(f"https://opensubtitles-v3.strem.io/subtitles/{query.kind}/{identity}.json")
    result = []
    for item in data.get("subtitles", []):
        if not any(language_matches(item.get("lang", ""), lang) for lang in languages):
            continue
        result.append(
            Candidate(
                "opensubtitles",
                str(item["id"]),
                query.title,
                language_code(item["lang"]),
                item["url"],
                item.get("subtitleFileName") or item.get("movieReleaseName", ""),
                query.year,
                item.get("season") or None,
                item.get("episode") or None,
                bool(item.get("hearingImpaired", False)),
                extra={"encoding": item.get("SubEncoding")},
            )
        )
    return result


def gestdown(query: Query, languages: list[str], net: Network) -> list[Candidate]:
    if query.episode is None:
        return []
    data = net.json(f"https://api.gestdown.info/shows/search/{quote(query.title, safe='')}", ttl=86400)
    shows = sorted(data.get("shows", []), key=lambda s: -similarity(query.title, s["name"]))
    if not shows or similarity(query.title, shows[0]["name"]) < 85:
        return []
    show = shows[0]
    result = []
    for lang in languages:
        alpha3 = Language.fromietf(lang).alpha3
        data = net.json(
            f"https://api.gestdown.info/subtitles/get/{show['id']}/{query.season}/{query.episode}/{alpha3}"
        )
        for item in data.get("matchingSubtitles", []):
            if not item.get("completed", True):
                continue
            release = f"{show['name']} S{query.season:02}E{query.episode:02} {item.get('version', '')}"
            result.append(
                Candidate(
                    "gestdown",
                    str(item["subtitleId"]),
                    show["name"],
                    lang,
                    urljoin("https://api.gestdown.info", item["downloadUri"]),
                    release,
                    query.year,
                    query.season,
                    query.episode,
                    item.get("hearingImpaired", False),
                    item.get("downloadCount", 0),
                )
            )
    return result


def yify(query: Query, languages: list[str], net: Network) -> list[Candidate]:
    if not query.imdb or query.episode is not None:
        return []
    soup = BeautifulSoup(net.fetch(f"https://yifysubtitles.ch/movie-imdb/{query.imdb}"), "html.parser")
    result = []
    for row in soup.select("tr[data-id]"):
        language = row.select_one(".sub-lang")
        link = row.select_one('a[href^="/subtitles/"]')
        if not language or not link:
            continue
        try:
            code = language_code(language.get_text(strip=True))
        except ValueError:
            continue
        if not any(language_matches(code, lang) for lang in languages):
            continue
        release = link.get_text(" ", strip=True).removeprefix("subtitle ")
        result.append(
            Candidate(
                "yify",
                str(row["data-id"]),
                query.title,
                code,
                urljoin("https://yifysubtitles.ch", link["href"]),
                release,
                query.year,
                extra={"page": True},
            )
        )
    return result


def subdl(query: Query, languages: list[str], net: Network) -> list[Candidate]:
    key = os.environ.get("SUBDL_API_KEY")
    if not key:
        return []
    params = {"api_key": key, "languages": ",".join(languages), "subs_per_page": 30}
    params["imdb_id" if query.imdb else "film_name"] = query.imdb or query.title
    if query.episode is not None:
        params.update(season_number=query.season, episode_number=query.episode, type="tv")
    if query.year:
        params["year"] = query.year
    data = net.json("https://api.subdl.com/api/v1/subtitles", params=params)
    if data.get("status") is False:
        raise ValueError("SubDL rejected the search; check your API key and quota.")
    result = []
    for item in data.get("subtitles", []):
        try:
            language = language_code(item.get("language", item.get("lang", "")))
        except ValueError:
            continue
        if not any(language_matches(language, lang) for lang in languages):
            continue
        result.append(
            Candidate(
                "subdl",
                str(item.get("sd_id", item.get("url"))),
                query.title,
                language,
                urljoin("https://dl.subdl.com", item["url"]),
                item.get("release_name") or item.get("name", ""),
                query.year,
                item.get("season"),
                item.get("episode"),
            )
        )
    return result


def search(
    query: Query, languages: list[str], net: Network, providers: list[str] | None = None, progress=None
) -> tuple[list[Candidate], list[str]]:
    selected = providers or ["opensubtitles", "gestdown", "yify"]
    if providers is None and os.environ.get("SUBDL_API_KEY"):
        selected.append("subdl")
    functions = {"opensubtitles": opensubtitles, "gestdown": gestdown, "yify": yify, "subdl": subdl}
    found, errors = [], []
    with ThreadPoolExecutor(max_workers=len(selected) or 1) as pool:
        futures = {pool.submit(functions[name], query, languages, net): name for name in selected}
        for future in as_completed(futures):
            name = futures[future]
            try:
                items = future.result()
                found.extend(items)
                if progress:
                    progress(name, len(items), None)
            except Exception as exc:
                message = error_message(exc)
                errors.append(f"{name}: {message}")
                if progress:
                    progress(name, 0, message)
    return rank_candidates(found, query), errors
