import io
import zipfile

import pytest

from easysubs.download import destination, normalize, save_atomic, unpack
from easysubs.models import Candidate, Query, Title, language_code, language_matches, rank_candidates
from easysubs.service import confident_title

SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello world.\n"


def test_filename_release_and_episode(tmp_path):
    video = tmp_path / "Breaking.Bad.S02E03.1080p.BluRay.x264-GROUP.mkv"
    video.touch()
    q = Query.parse(str(video))
    assert (q.title, q.season, q.episode) == ("Breaking Bad", 2, 3)
    assert q.release["screen_size"] == "1080p"
    assert destination(q, "eng") == tmp_path / (video.stem + ".en.srt")


def test_natural_episode_and_overrides():
    q = Query.parse("breaking bad season 2 episode 3")
    assert (q.season, q.episode) == (2, 3)
    q = Query.parse("movie", episode=4)
    assert (q.season, q.episode) == (1, 4)
    with pytest.raises(ValueError):
        Query.parse("breaking bad s01")


def test_fuzzy_matching_never_rescues_wrong_episode():
    q = Query.parse("Breaking.Bad.S02E03.1080p.BluRay.x264-GROUP")
    wrong = Candidate("x", "1", "Breaking Bad", "en", "", "Breaking.Bad.S02E04.1080p.BluRay.x264-GROUP")
    right = Candidate("x", "2", "Breaking Bad", "en", "", "Breaking.Bad.S02E03.WEB-DL")
    assert [c.id for c in rank_candidates([wrong, right], q)] == ["2"]


def test_release_match_beats_generic_and_deduplicates():
    q = Query.parse("Arrival.2016.1080p.BluRay.x264-SPARKS")
    generic = Candidate("x", "1", "Arrival", "en", "", "Arrival 2016", 2016)
    exact = Candidate("x", "2", "Arrival", "en", "", "Arrival.2016.1080p.BluRay.x264-SPARKS", 2016)
    wrong = Candidate("x", "3", "Arrival", "en", "", "Arrival", 1996)
    results = rank_candidates([generic, exact, wrong, exact], q)
    assert [c.id for c in results] == ["2", "1"]
    assert "release group" in results[0].reasons


def test_title_ambiguity_and_typo():
    q = Query.parse("interstllar")
    good = Title("Interstellar", "tt0816692", "movie", 2014, 95)
    assert confident_title([good], q) == good
    assert confident_title([good, Title("Interstellar", "tt123456", "movie", 2020, 94)], q) is None


@pytest.mark.parametrize(
    "value,wanted",
    [("English", "en"), ("eng", "en"), ("Hindi", "hi"), ("pob", "pt-BR"), ("fre", "fr"), ("ja", "ja")],
)
def test_languages(value, wanted):
    assert language_code(value) == wanted


def test_regional_language():
    assert language_matches("pob", "pt")
    assert not language_matches("pt", "pt-BR")
    assert not language_matches("made-up", "en")


def test_normalize_rejects_webpage_and_empty():
    assert "Hello world." in normalize(SRT)
    with pytest.raises(ValueError):
        normalize(b"<!DOCTYPE html><html>Rate limit</html>")
    with pytest.raises(ValueError):
        normalize(b"")
    assert "Hello" in normalize(b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n")


def zip_data(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


def test_archive_selects_episode_without_extracting_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = zip_data({"../../Show.S01E02.en.srt": SRT, "Show.S01E03.en.srt": SRT.replace(b"Hello", b"Wrong")})
    assert unpack(data, Query.parse("Show S01E02"), "en") == SRT
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError):
        unpack(data, Query.parse("Show S01E04"), "en")


def test_archive_wrong_language():
    data = zip_data({"Show.S01E02.fr.srt": SRT})
    with pytest.raises(ValueError):
        unpack(data, Query.parse("Show S01E02"), "en")


def test_atomic_write_protects_existing_and_cleans_temp(tmp_path):
    path = tmp_path / "movie.en.srt"
    save_atomic(path, "original")
    with pytest.raises(FileExistsError):
        save_atomic(path, "replacement")
    assert path.read_text() == "original"
    save_atomic(path, "replacement", force=True)
    assert path.read_text() == "replacement"
    assert list(tmp_path.iterdir()) == [path]


def test_nonfile_title_cannot_escape_output(tmp_path):
    target = destination(Query("", "../../evil"), "en", tmp_path)
    assert target.parent == tmp_path
