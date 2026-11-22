# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import json
import shutil
import subprocess
from urllib.error import HTTPError

import pytest

from gentoodo.collectors import codeberg_prs
from gentoodo.collectors.codeberg_prs import CodebergPRsCollector, next_link
from gentoodo.config import Config
from gentoodo.maintainers import Maintainer

API = "https://codeberg.org/api/v1/repos/gentoo/gentoo/pulls"


@pytest.mark.parametrize("header,expected", [
    (f'<{API}?page=2>; rel="next",<{API}?page=5>; rel="last"', f"{API}?page=2"),
    (f'<{API}?page=1>; rel="first", <{API}?page=3>; rel="next"', f"{API}?page=3"),
    (f'<{API}?page=2>;rel=next', f"{API}?page=2"),
    (f'<{API}?page=1>; rel="first",<{API}?page=4>; rel="prev"', None),
    ("", None),
    (None, None),
])
def test_next_link(header, expected):
    assert next_link(header) == expected


def git(path, *args):
    return subprocess.run(["git", "-C", path, "-c", "user.name=A", "-c",
                           "user.email=a@gentoo.org", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def commit(work, *names, rename=None):
    for name in names:
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(name)
    git(work, "add", "-A")
    if rename:
        git(work, "mv", *rename)
    git(work, "commit", "-q", "-m", "change")
    return git(work, "rev-parse", "HEAD")


class Forge:
    """A Forgejo forge at a file: URL, with gentoo/gentoo, a working tree to
    make its pull requests in, and a partial clone of it like that in
    cache_dir."""

    def __init__(self, tmp_path):
        self.url = (tmp_path / "forge").as_uri()
        self.upstream = tmp_path / "forge" / "gentoo" / "gentoo.git"
        self.work = tmp_path / "work"
        self.clone = tmp_path / "clone.git"
        git(tmp_path, "init", "-q", "--bare", str(self.upstream))
        git(self.upstream, "config", "uploadpack.allowFilter", "true")
        git(tmp_path, "init", "-q", "-b", "master", str(self.work))
        commit(self.work, "app-misc/foo/foo-1.ebuild", "app-misc/bar/bar-1.ebuild")
        self.push_master()
        git(tmp_path, "clone", "-q", "--bare", "--filter=blob:none",
            self.upstream.as_uri(), str(self.clone))

    def push_master(self):
        git(self.work, "push", "-q", str(self.upstream), "master")

    def pr(self, number, *names, rename=None):
        """Push a pull request changing `names`, and return it as the API does."""
        merge_base = git(self.work, "rev-parse", "master")
        git(self.work, "checkout", "-q", "-b", f"pr{number}")
        sha = commit(self.work, *names, rename=rename)
        git(self.work, "push", "-q", str(self.upstream), f"HEAD:refs/pull/{number}/head")
        git(self.work, "checkout", "-q", "master")
        return {"number": number, "title": "Bump", "html_url": f"https://example.org/{number}",
                "merge_base": merge_base,
                "head": {"ref": f"pr{number}", "sha": sha}}


@pytest.fixture
def forge(tmp_path, git_env, monkeypatch):
    forge = Forge(tmp_path)
    monkeypatch.setattr(codeberg_prs, "URL", forge.url)
    return forge


def setup(monkeypatch, forge, responses, **options):
    """Answer each API URL with a list of responses, each a JSON value and an
    optional next URL."""
    calls = []
    responses = {url: list(answers) for url, answers in responses.items()}

    def fetch_with_headers(url):
        url = url.replace(forge.url, "https://codeberg.org")
        calls.append(url)
        response = responses[url].pop(0)
        if isinstance(response, Exception):
            raise response
        value, next_url = response if isinstance(response, tuple) else (response, None)
        headers = {"Link": f'<{next_url}>; rel="next"'} if next_url else {}
        return json.dumps(value).encode(), headers

    monkeypatch.setattr(codeberg_prs, "fetch_with_headers", fetch_with_headers)
    collector = CodebergPRsCollector(Config(gentoo_repo=forge.clone, **options))
    asyncio.run(collector.setup())
    return collector, calls


def test_pages(monkeypatch, forge):
    prs = [forge.pr(1, "a/b/b-1.ebuild"), forge.pr(2, "c/d/d-1.ebuild"),
           forge.pr(3, "e/f/f-1.ebuild")]
    collector, calls = setup(monkeypatch, forge, {
        f"{API}?state=open&limit=50": [(prs[:2], f"{API}?page=2")],
        f"{API}?page=2": [[prs[2], None]],
    })
    assert set(collector.items) == {"a/b", "c/d", "e/f"}
    # Only the list of pull requests, not their files, from the API
    assert calls == [f"{API}?state=open&limit=50", f"{API}?page=2"]


def test_files_since_merge_base(monkeypatch, forge):
    pr = forge.pr(1, "app-misc/foo/foo-2.ebuild")
    # Changed on master since, which is not the pull request's change
    commit(forge.work, "app-misc/bar/bar-2.ebuild")
    forge.push_master()

    collector, _ = setup(monkeypatch, forge, {f"{API}?state=open&limit=50": [[pr]]})

    assert set(collector.items) == {"app-misc/foo"}


def test_rename_lists_both_names(monkeypatch, forge):
    def missing():
        return git(forge.clone, "rev-list", "--objects", "--missing=print",
                   "master").count("?")

    pr = forge.pr(1, rename=("app-misc/foo", "app-misc/baz"))
    before = missing()
    collector, _ = setup(monkeypatch, forge, {f"{API}?state=open&limit=50": [[pr]]})
    assert set(collector.items) == {"app-misc/foo", "app-misc/baz"}
    # Without fetching the contents of the files to detect the rename
    assert before and missing() == before


def test_pushed_since_listed(monkeypatch, forge, caplog):
    prs = [forge.pr(1, "a/b/b-1.ebuild"), forge.pr(2, "c/d/d-1.ebuild")]
    prs[1]["head"]["sha"] = "0" * 40
    collector, _ = setup(monkeypatch, forge, {f"{API}?state=open&limit=50": [prs]})
    assert set(collector.items) == {"a/b"}
    assert "Skipping Codeberg PR #2" in caplog.text


def test_no_prs(monkeypatch, forge):
    other = "https://codeberg.org/api/v1/repos/larry/overlay/pulls"
    _, calls = setup(monkeypatch, forge, {f"{other}?state=open&limit=50": [[]]},
                     codeberg_repo="larry/overlay")
    # Nothing fetched from larry/overlay, which does not exist
    assert calls == [f"{other}?state=open&limit=50"]


def test_sorted_by_number(monkeypatch, forge):
    # Packages in metadata.xml order, not alphabetical, and a PR on two of them
    packages = {5: ["z/z", "a/a"], 3: ["a/a"], 4: ["m/m"], 1: ["m/m"], 2: ["z/z"]}
    prs = [forge.pr(n, *(f"{p}/{n}.ebuild" for p in pkgs)) for n, pkgs in packages.items()]
    collector, _ = setup(monkeypatch, forge, {f"{API}?state=open&limit=50": [prs]})
    maintainer = Maintainer("x@gentoo.org", packages=["m/m", "z/z", "a/a"])

    [section] = asyncio.run(collector.collect(maintainer))

    assert [item.ref for item in section.items] == [f"PR #{n}" for n in range(1, 6)]


def test_closed_since_listed(monkeypatch, forge, caplog):
    prs = [forge.pr(n, f"a/b{n}/b{n}-1.ebuild") for n in (1, 2, 3)]
    # Closed, with their heads deleted, since they were listed
    for n in (1, 3):
        git(forge.upstream, "update-ref", "-d", f"refs/pull/{n}/head")
    collector, _ = setup(monkeypatch, forge, {f"{API}?state=open&limit=50": [prs]})
    assert set(collector.items) == {"a/b2"}
    assert "Skipping Codeberg PR #1" in caplog.text
    assert "Skipping Codeberg PR #3" in caplog.text


@pytest.mark.parametrize("break_it", ["api", "git"])
def test_failure_is_a_notice(monkeypatch, forge, break_it):
    if break_it == "api":
        response = HTTPError(API, 404, "Not Found", {}, None)
    else:
        response = [forge.pr(1, "a/b/b-1.ebuild")]
        shutil.rmtree(forge.upstream)
    collector, _ = setup(monkeypatch, forge, {f"{API}?state=open&limit=50": [response]})
    maintainer = Maintainer("x11@gentoo.org", packages=["x11-libs/libdrm"])

    [section] = asyncio.run(collector.collect(maintainer))

    assert collector.items is None
    assert section.notice
    assert section.title == "Open Codeberg Pull Requests"
    assert section.text == "Could not be fetched from Codeberg."
