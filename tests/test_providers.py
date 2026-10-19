from easysubs.models import Query
from easysubs.net import Network
from easysubs.providers import opensubtitles, search, yify


class FakeNetwork:
    def json(self, url, **kwargs):
        return {
            "subtitles": [
                {
                    "id": "1",
                    "lang": "eng",
                    "url": "https://example.org/1.srt",
                    "subtitleFileName": "Film.2020.srt",
                },
                {"id": "2", "lang": "fra", "url": "https://example.org/2.srt"},
            ]
        }

    def fetch(self, url, **kwargs):
        return b"""<table><tr data-id="123"><td><span class="sub-lang">English</span></td>
        <td><a href="/subtitles/film-en-123"><span>subtitle</span> Film.2020.BluRay</a></td></tr></table>"""


def test_opensubtitles_language_and_release():
    found = opensubtitles(Query("Film", "Film", 2020, imdb="tt123456"), ["en"], FakeNetwork())
    assert len(found) == 1
    assert found[0].release == "Film.2020.srt"


def test_yify_html_parser():
    found = yify(Query("Film", "Film", 2020, imdb="tt123456"), ["en"], FakeNetwork())
    assert len(found) == 1
    assert found[0].url == "https://yifysubtitles.ch/subtitles/film-en-123"
    assert found[0].release == "Film.2020.BluRay"


def test_source_failure_does_not_discard_working_source(monkeypatch):
    monkeypatch.setattr(
        "easysubs.providers.yify", lambda *args: (_ for _ in ()).throw(ValueError("unavailable"))
    )
    found, warnings = search(
        Query("Film", "Film", 2020, imdb="tt123456"), ["en"], FakeNetwork(), ["opensubtitles", "yify"]
    )
    assert len(found) == 1
    assert warnings == ["yify: unavailable"]


def test_cache_skips_network(tmp_path, monkeypatch):
    net = Network()
    net.cache_dir = tmp_path
    calls = []

    def fetch(*args, **kwargs):
        calls.append(args)
        return b'{"result": 1}'

    monkeypatch.setattr(net, "fetch", fetch)
    assert net.json("https://example.org") == net.json("https://example.org")
    assert len(calls) == 1
    net.json("https://example.org", params={"api_key": "secret"})
    assert len(calls) == 2
    assert len(list(tmp_path.iterdir())) == 1


def test_yify_download_keeps_page_referer(tmp_path):
    import io
    import zipfile

    from easysubs.download import download
    from easysubs.models import Candidate

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Film.2020.en.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    calls = []

    class NetworkWithReferer:
        def fetch(self, url, headers=None):
            calls.append((url, headers))
            if url.endswith(".zip"):
                assert headers == {"Referer": "https://yifysubtitles.ch/subtitles/film-en-123"}
                return buffer.getvalue()
            return b'<a href="/subtitle/film-en-123.zip">Download</a>'

    c = Candidate(
        "yify", "123", "Film", "en", "https://yifysubtitles.ch/subtitles/film-en-123", extra={"page": True}
    )
    path = download(c, Query("Film", "Film", 2020), NetworkWithReferer(), tmp_path)
    assert path.name == "Film (2020).en.srt"
    assert "Hello" in path.read_text()
