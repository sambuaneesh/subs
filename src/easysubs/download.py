from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import pysubs2
from bs4 import BeautifulSoup
from charset_normalizer import from_bytes
from guessit import guessit

from .models import SUB_EXTENSIONS, Candidate, Query, language_code, language_matches, similarity
from .net import MAX_BYTES, Network


def destination(query: Query, language: str, output: Path | None = None) -> Path:
    if query.path:
        folder = output or query.path.parent
        stem = query.path.stem
    else:
        folder = output or Path.cwd()
        stem = query.title
        if query.year:
            stem += f" ({query.year})"
        if query.episode is not None:
            stem += f" S{query.season:02}E{query.episode:02}"
        stem = re.sub(r'[\\/\x00-\x1f:*?"<>|]', "_", stem).strip(". ")[:180] or "subtitle"
    return folder / f"{stem}.{language_code(language)}.srt"


def normalize(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise ValueError("Subtitle exceeds the 20 MB limit.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = from_bytes(data).best()
        if decoded is None:
            raise ValueError("Could not decode the subtitle.") from None
        text = str(decoded)
    if re.search(r"<(?:!doctype\s+html|html)\b", text[:1000], re.I):
        raise ValueError("Source returned a web page instead of subtitles.")
    try:
        subs = pysubs2.SSAFile.from_string(text)
    except Exception as exc:
        raise ValueError("Download is not a supported text subtitle.") from exc
    if not subs or not any(event.text.strip() for event in subs):
        raise ValueError("Downloaded subtitle contains no captions.")
    return subs.to_string("srt")


def unpack(data: bytes, query: Query, language: str) -> bytes:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        eligible = []
        for member in archive.infolist():
            name = Path(member.filename).name
            if member.is_dir() or Path(name).suffix.lower() not in SUB_EXTENSIONS:
                continue
            if member.file_size > MAX_BYTES:
                continue
            guessed = guessit(name)
            if query.episode is not None:
                if guessed.get("episode") not in (None, query.episode):
                    continue
                if guessed.get("season") not in (None, query.season):
                    continue
            # Prefer the requested episode and language inside season/multilanguage ZIPs.
            score = similarity(query.path.stem if query.path else query.title, name)
            if query.episode is not None and guessed.get("episode") == query.episode:
                score += 100
            guessed_languages = guessed.get("subtitle_language", guessed.get("language", []))
            if not isinstance(guessed_languages, list):
                guessed_languages = [guessed_languages]
            if guessed_languages:
                if not any(language_matches(str(lang), language) for lang in guessed_languages):
                    continue
                score += 30
            eligible.append((score, member))
        if not eligible:
            raise ValueError("Archive has no subtitle matching this episode/language.")
        eligible.sort(key=lambda pair: -pair[0])
        if len(eligible) > 1 and eligible[0][0] == eligible[1][0]:
            raise ValueError("Archive has multiple equally matching files; choose another release.")
        # Read bytes only: never extract archive paths onto the filesystem.
        with archive.open(eligible[0][1]) as member:
            content = member.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ValueError("Unpacked subtitle exceeds the size limit.")
        return content


def save_atomic(path: Path, text: str, force: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".subs-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if force:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise FileExistsError(f"Already exists: {path.name}. Use --force to replace it.") from None
        return path.resolve()
    finally:
        Path(temporary).unlink(missing_ok=True)


def embedded(query: Query, languages: list[str]) -> list[Candidate]:
    if not query.path or not shutil.which("ffprobe"):
        return []
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_streams",
                "-of",
                "json",
                str(query.path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except (subprocess.SubprocessError, ValueError):
        return []
    candidates = []
    for stream in streams:
        if stream.get("codec_name") not in {"subrip", "ass", "ssa", "webvtt", "mov_text", "text"}:
            continue
        lang = stream.get("tags", {}).get("language", "und")
        if not any(language_matches(lang, requested) for requested in languages):
            continue
        candidates.append(
            Candidate(
                "embedded",
                str(stream["index"]),
                query.title,
                language_code(lang),
                "",
                stream.get("tags", {}).get("title") or "Text track already in this video",
                query.year,
                query.season,
                query.episode,
                extra={"stream": stream["index"]},
            )
        )
    return candidates


def url_candidates(
    url: str, languages: list[str], auto: bool, timeout: float
) -> tuple[Query, list[Candidate]]:
    from yt_dlp import YoutubeDL

    class QuietLogger:
        def debug(self, message):
            pass

        def warning(self, message):
            pass

        def error(self, message):
            pass

    with YoutubeDL(
        {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": timeout,
            "retries": 1,
            "logger": QuietLogger(),
        }
    ) as downloader:
        info = downloader.extract_info(url, download=False)
    if not info or info.get("_type") in {"playlist", "multi_video"}:
        raise ValueError("Pass a single video URL, not a playlist.")
    query = Query(url, info.get("title", "video"))
    candidates = []
    for automatic, field in ((False, "subtitles"), (True, "automatic_captions")):
        if automatic and not auto:
            continue
        for lang, tracks in (info.get(field) or {}).items():
            if not tracks or not any(language_matches(lang, requested) for requested in languages):
                continue
            candidates.append(
                Candidate(
                    "yt-dlp",
                    f"{lang}:{automatic}",
                    query.title,
                    language_code(lang),
                    url,
                    "Auto-generated captions" if automatic else "Human subtitles",
                    score=70 if automatic else 100,
                    reasons=["video captions"],
                    extra={"automatic": automatic, "track": lang},
                )
            )
    return query, sorted(candidates, key=lambda c: -c.score)


def download(
    candidate: Candidate, query: Query, net: Network, output: Path | None = None, force: bool = False
) -> Path:
    target = destination(query, candidate.language, output)
    if target.exists() and not force:
        raise FileExistsError(f"Already exists: {target.name}. Use --force to replace it.")
    if candidate.provider in {"embedded", "yt-dlp"}:
        with tempfile.TemporaryDirectory(prefix="subs-") as temporary:
            folder = Path(temporary)
            if candidate.provider == "embedded":
                if not shutil.which("ffmpeg"):
                    raise ValueError("Install ffmpeg to extract embedded subtitles.")
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-nostdin",
                        "-i",
                        str(query.path),
                        "-map",
                        f"0:{candidate.extra['stream']}",
                        str(folder / "captions.srt"),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
            else:
                command = [
                    sys.executable,
                    "-m",
                    "yt_dlp",
                    "--ignore-config",
                    "--no-playlist",
                    "--skip-download",
                    "--write-auto-subs" if candidate.extra["automatic"] else "--write-subs",
                    "--sub-langs",
                    "^" + re.escape(candidate.extra["track"]) + "$",
                    "--sub-format",
                    "srt/vtt/ass/best",
                    "--socket-timeout",
                    str(net.timeout),
                    "--retries",
                    "1",
                    "-o",
                    str(folder / "captions.%(ext)s"),
                    "--",
                    candidate.url,
                ]
                process = subprocess.run(command, capture_output=True, text=True, timeout=180)
                if process.returncode:
                    raise ValueError(
                        "Caption download failed. The site may require login or be rate-limiting requests."
                    )
            files = [p for p in folder.iterdir() if p.suffix.lower() in SUB_EXTENSIONS]
            if not files:
                raise ValueError("No subtitle file was produced.")
            data = files[0].read_bytes()
    else:
        url = candidate.url
        if candidate.extra.get("page"):
            page = BeautifulSoup(net.fetch(url), "html.parser")
            link = page.select_one('a[href*=".zip"]')
            if not link:
                raise ValueError("No subtitle download link found on source page.")
            url = urljoin(url, link["href"])
        headers = {"Referer": candidate.url} if candidate.extra.get("page") else None
        data = unpack(net.fetch(url, headers=headers), query, candidate.language)
    return save_atomic(target, normalize(data), force)
