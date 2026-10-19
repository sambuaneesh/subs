from argparse import Namespace

import httpx

from easysubs.cli import main, run_one
from easysubs.models import Candidate
from easysubs.net import Network


def test_best_moves_to_other_source_after_access_denied(monkeypatch, tmp_path):
    candidates = [Candidate("yify", str(i), "Arrival", "en", "") for i in range(8)]
    candidates.append(Candidate("opensubtitles", "9", "Arrival", "en", ""))
    monkeypatch.setattr("easysubs.cli.resolve", lambda query, net: (query, []))
    monkeypatch.setattr("easysubs.cli.collect", lambda *args: (candidates, []))
    calls = []

    def download(candidate, *args):
        calls.append(candidate.provider)
        if candidate.provider == "yify":
            request = httpx.Request("GET", "https://example.org")
            raise httpx.HTTPStatusError(
                "denied", request=request, response=httpx.Response(403, request=request)
            )
        return tmp_path / "Arrival.en.srt"

    monkeypatch.setattr("easysubs.cli.download", download)
    args = Namespace(
        title=None,
        imdb=None,
        year=None,
        season=None,
        episode=None,
        best=True,
        list=False,
        json=False,
        force=False,
        output=tmp_path,
        auto_subs=False,
    )
    assert run_one("Arrival", args, ["en"], Network(), None, interactive=False)
    assert calls == ["yify", "opensubtitles"]


def test_missing_path_does_not_become_title_search(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setattr("easysubs.cli.read_config", lambda: {})
    monkeypatch.setattr(
        "easysubs.cli.run_one", lambda *args: pytest.fail("Missing file became a title search")
    )
    with pytest.raises(SystemExit) as error:
        main([str(tmp_path / "missing.mkv"), "--best"])
    assert error.value.code == 1
