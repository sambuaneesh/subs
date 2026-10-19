from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

from . import __version__
from .download import destination, download, url_candidates
from .models import VIDEO_EXTENSIONS, Query, language_code, language_matches
from .net import Network, error_message
from .providers import SOURCES
from .service import collect, resolve

console = Console(stderr=True, highlight=False)


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "easysubs" / "config.json"


def read_config():
    try:
        value = json.loads(config_path().read_text())
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not read {config_path()}: {exc}") from exc


def parser():
    p = argparse.ArgumentParser(
        prog="subs",
        description="Find subtitles. Pick one. Play your video.",
        epilog="Examples: subs movie.mkv | subs 'breaking bad s01e01' | subs . --best | subs config -l hi",
    )
    p.add_argument("target", nargs="*", help="video filename, folder, movie/show title, or video URL")
    p.add_argument("-l", "--language", help="language(s), comma separated; default: en")
    p.add_argument(
        "-b", "--best", action="store_true", help="skip the TUI; download the top match per language"
    )
    p.add_argument("--list", action="store_true", help="list matches without downloading")
    p.add_argument("--json", action="store_true", help="list matches as JSON (no download)")
    p.add_argument("-o", "--output", type=Path, help="output directory; default: beside the video")
    p.add_argument("--title", help="override the title parsed from a filename")
    p.add_argument("--imdb", help="exact IMDb ID, e.g. tt0816692")
    p.add_argument("--year", type=int, help="release year")
    p.add_argument("--season", type=int, help="TV season")
    p.add_argument("--episode", type=int, help="TV episode (defaults season to 1)")
    p.add_argument("--provider", help="comma-separated sources: " + ", ".join(SOURCES))
    p.add_argument("--auto-subs", action="store_true", help="also offer auto-generated captions for URLs")
    p.add_argument("--force", action="store_true", help="replace an existing subtitle")
    p.add_argument(
        "--timeout", type=float, default=12, help="per-request network timeout in seconds (default: 12)"
    )
    p.add_argument("--no-cache", action="store_true", help="search sources again, bypassing cached results")
    p.add_argument("--version", action="version", version=f"subs {__version__}")
    return p


def display(candidates):
    table = Table(box=None, padding=(0, 1))
    for col in ("Match", "Language", "Source", "Release"):
        table.add_column(col)
    for c in candidates:
        table.add_row(f"{c.score:.0f}", c.language, c.provider, c.release or c.title)
    Console(highlight=False).print(table)


def run_one(raw, args, languages, net, providers, interactive=True):
    query = (
        Query(raw, raw)
        if raw.startswith(("https://", "http://"))
        else Query.parse(
            raw,
            allow_partial=not any((args.best, args.list, args.json)),
            title=args.title,
            imdb=args.imdb,
            year=args.year,
            season=args.season,
            episode=args.episode,
        )
    )
    if args.imdb and not re.fullmatch(r"tt\d{5,12}", args.imdb):
        raise ValueError("IMDb ID must look like tt0816692.")
    if not any((args.best, args.list, args.json)):
        if not interactive or not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError("Use --best to download or --list / --json to list matches outside a terminal.")
        from .tui import SubsApp

        result = SubsApp(
            query,
            languages,
            net,
            providers=providers,
            output=args.output,
            force=args.force,
            auto_subs=args.auto_subs,
        ).run()
        if result:
            console.print("✓ Saved ", str(result), style="green", markup=False)
        return result is not None
    warnings = []
    # Skip already-saved languages before making network requests in batch/best mode.
    pending = languages
    if args.best and query.path and not args.force:
        pending = [lang for lang in languages if not destination(query, lang, args.output).exists()]
        if not pending:
            console.print(f"Already has subtitles: {query.path.name}", markup=False)
            return True
    with console.status("Finding subtitles…") if not args.json else nullcontext():
        if raw.startswith(("http://", "https://")):
            query, candidates = url_candidates(raw, pending, args.auto_subs, net.timeout)
        else:
            query, warnings = resolve(query, net)
            candidates, extra = collect(query, pending, net, providers)
            warnings.extend(extra)
    if args.json:
        print(
            json.dumps(
                {"query": query.title, "results": [asdict(c) for c in candidates], "warnings": warnings},
                ensure_ascii=False,
                indent=2,
            )
        )
        return bool(candidates)
    for warning in warnings:
        console.print(f"Source note: {warning}", style="dim", markup=False)
    if not candidates:
        raise ValueError(
            "No matching subtitles found. Try -l LANGUAGE, --title TITLE, or --auto-subs for URLs."
        )
    if args.list:
        display(candidates)
        return True
    success = True
    for lang in pending:
        choices = [c for c in candidates if language_matches(c.language, lang)]
        if not choices:
            console.print(f"No subtitles found for {lang}.", style="yellow", markup=False)
            success = False
            continue
        saved = False
        blocked_sources = set()
        attempts = 0
        for c in choices:
            if c.provider in blocked_sources:
                continue
            if attempts >= 5:
                break
            attempts += 1
            try:
                path = download(c, query, net, args.output, args.force)
                console.print(f"✓ Saved {path}  ({c.provider})", style="green", markup=False)
                saved = True
                break
            except FileExistsError as exc:
                console.print(str(exc), style="yellow", markup=False)
                saved = True
                break
            except Exception as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403, 429}:
                    blocked_sources.add(c.provider)
                console.print(
                    f"{c.provider}: {error_message(exc)}; trying next match…", style="dim", markup=False
                )
        success &= saved
    return success


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.timeout <= 0 or args.timeout > 120:
            raise ValueError("--timeout must be between 0 and 120 seconds.")
        if args.season is not None and args.season < 0 or args.episode is not None and args.episode < 1:
            raise ValueError("Season must be >= 0 and episode >= 1.")
        config = read_config()
        languages = list(
            dict.fromkeys(
                language_code(lang) for lang in (args.language or config.get("language", "en")).split(",")
            )
        )
        raw = " ".join(args.target)
        if raw == "config":
            if args.language:
                config["language"] = ",".join(languages)
                path = config_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(config, indent=2) + "\n")
                console.print(f"Default language: {config['language']}", markup=False)
            else:
                console.print(
                    f"Default language: {','.join(languages)}\nConfig: {config_path()}", markup=False
                )
            return
        if raw in {"sources", "doctor"}:
            for name, description in SOURCES.items():
                console.print(f"{name:14} {description}", markup=False)
            if raw == "doctor":
                console.print(
                    f"\nPython: {sys.version.split()[0]}\nffmpeg: {shutil.which('ffmpeg') or 'not installed'}"
                    f"\nSubDL key: {'set' if os.environ.get('SUBDL_API_KEY') else 'not set (optional)'}"
                    f"\nDefault language: {','.join(languages)}\nConfig: {config_path()}",
                    markup=False,
                )
            return
        providers = args.provider.split(",") if args.provider else None
        if providers and any(p not in SOURCES for p in providers):
            raise ValueError("Unknown source. Available: " + ", ".join(SOURCES))
        if providers and "subdl" in providers and not os.environ.get("SUBDL_API_KEY"):
            raise ValueError("Set SUBDL_API_KEY to use the optional SubDL source.")
        if not raw:
            videos = sorted(
                p for p in Path.cwd().iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
            )
            if len(videos) == 1:
                raw = str(videos[0])
            elif videos:
                raise ValueError("Specify a filename, or use 'subs . --best' for all videos in this folder.")
            elif sys.stdin.isatty():
                raw = input("Movie, episode, file, or URL: ").strip()
            else:
                parser().print_help()
                raise SystemExit(2)
        if not raw:
            raise ValueError("Enter a title, filename, folder, or URL.")
        if not raw.startswith(("http://", "https://")):
            path = Path(raw).expanduser()
            if not path.exists() and (
                path.suffix.lower() in VIDEO_EXTENSIONS or raw.startswith(("/", "./", "../", "~/"))
            ):
                raise ValueError(f"File not found: {raw}")
            if path.is_dir():
                if not args.best or args.json or args.list:
                    raise ValueError(
                        "For a folder use 'subs . --best'. For the TUI, pass one video filename."
                    )
                if any((args.title, args.imdb, args.year, args.season is not None, args.episode is not None)):
                    raise ValueError(
                        "Title/episode overrides are for a single file; omit them for batch downloads."
                    )
                videos = sorted(
                    p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
                )
                if not videos:
                    raise ValueError("No video files found in that folder.")
                okay = 0
                for video in videos:
                    try:
                        okay += bool(
                            run_one(
                                str(video),
                                args,
                                languages,
                                Network(args.timeout, not args.no_cache),
                                providers,
                                interactive=False,
                            )
                        )
                    except Exception as exc:
                        console.print(f"{video.name}: {error_message(exc)}", style="yellow", markup=False)
                console.print(f"{okay}/{len(videos)} videos have subtitles.")
                raise SystemExit(0 if okay == len(videos) else 1)
        okay = run_one(raw, args, languages, Network(args.timeout, not args.no_cache), providers)
        raise SystemExit(0 if okay else 1)
    except (KeyboardInterrupt, EOFError):
        raise SystemExit(130) from None
    except Exception as exc:
        console.print(f"subs: {error_message(exc)}", style="red", markup=False)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
