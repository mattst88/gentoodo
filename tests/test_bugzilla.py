# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from gentoodo.collectors import bugzilla
from gentoodo.collectors.bugzilla import (
    BugsCollector, NewBugsCollector, by_assignee, summary_packages,
)
from gentoodo.config import Config
from gentoodo.maintainers import Maintainer
from gentoodo.util import previous_month


def bug(bug_id, assignee, time, component="Current packages", summary=None, cc=()):
    return {"id": bug_id, "summary": summary or f"bug {bug_id}", "component": component,
            "assigned_to": assignee, "cc": list(cc), "last_change_time": time}


def test_by_assignee():
    grouped = by_assignee([
        bug(1, "x11@gentoo.org", "2026-01-01T00:00:00Z"),
        bug(2, "X11@gentoo.org", "2026-01-01T00:00:00Z"),
        bug(3, "Somebody@example.com", "2026-01-01T00:00:00Z"),
    ])

    assert [b["id"] for b in grouped["x11@gentoo.org"]] == [1, 2]
    assert [b["id"] for b in grouped["somebody@example.com"]] == [3]


def test_search_sends_api_key(monkeypatch):
    urls = []

    def fetch(url):
        urls.append(url)
        return json.dumps({"bugs": [bug(1, "x11@gentoo.org", "2026-01-01T00:00:00Z")]}).encode()

    monkeypatch.setattr(bugzilla, "fetch", fetch)
    bugs = asyncio.run(bugzilla.search("secret", status="CONFIRMED"))
    assert [b["id"] for b in bugs] == [1]
    query = parse_qs(urlsplit(urls[0]).query)
    assert query["Bugzilla_api_key"] == ["secret"]
    assert query["status"] == ["CONFIRMED"]
    assert query["product"] == ["Gentoo Linux"]


def test_search_requires_api_key():
    with pytest.raises(RuntimeError, match="API key"):
        asyncio.run(bugzilla.search(None))


def test_search_reports_bugzilla_error(monkeypatch):
    def fetch(url):
        body = json.dumps({"message": "The API key you specified is invalid."})
        raise HTTPError(url, 400, "Bad Request", {}, io.BytesIO(body.encode()))

    monkeypatch.setattr(bugzilla, "fetch", fetch)
    with pytest.raises(RuntimeError, match="Bugzilla: The API key"):
        asyncio.run(bugzilla.search("bogus"))


def test_new_bugs_for_previous_month(monkeypatch):
    queries = []

    async def search(api_key, **params):
        queries.append(params)
        return [bug(len(queries), "x11@gentoo.org", "2026-01-01T00:00:00Z")]

    monkeypatch.setattr(bugzilla, "search", search)
    collector = NewBugsCollector(Config(gentoo_repo=Path("/nonexistent")))
    asyncio.run(collector.setup())

    first, last = previous_month(datetime.now(UTC).date())
    for query in queries:
        assert query["chfieldfrom"] == first.isoformat()
        assert query["chfieldto"] == last.isoformat()

    month = first.strftime("%B %Y")
    new, closed = asyncio.run(collector.collect(Maintainer("x11@gentoo.org")))
    assert new.title == f"Bugs Filed in {month}"
    assert closed.title == f"Bugs Closed in {month}"
    assert [item.ref for item in new.items + closed.items] == ["Bug #1", "Bug #2"]


def test_arch_bugs_counted_and_linked(monkeypatch):
    async def search(api_key, **params):
        return [
            bug(1, "x11@gentoo.org", "2026-01-01T00:00:00Z"),
            bug(2, "x11@gentoo.org", "2026-01-02T00:00:00Z", "Stabilization"),
            bug(3, "x11@gentoo.org", "2026-01-03T00:00:00Z", "Keywording"),
            bug(4, "larry@gentoo.org", "2026-01-04T00:00:00Z", "Stabilization"),
        ]

    monkeypatch.setattr(bugzilla, "search", search)
    collector = BugsCollector(Config(gentoo_repo=Path("/nonexistent")))
    asyncio.run(collector.setup())

    [x11] = asyncio.run(collector.collect(Maintainer("x11@gentoo.org")))
    assert [item.ref for item in x11.items] == ["Bug #1"]
    assert x11.items[0].text == ": bug 1 (last changed 2026-01-01)"
    assert x11.omitted == "2 stabilization and keywording bugs not shown"
    assert x11.omitted_url == "https://bugs.gentoo.org/buglist.cgi?bug_id=2,3"

    [larry] = asyncio.run(collector.collect(Maintainer("larry@gentoo.org")))
    assert larry.items == []
    assert larry.omitted == "1 stabilization and keywording bug not shown"
    assert larry.omitted_url == "https://bugs.gentoo.org/buglist.cgi?bug_id=4"


def test_maintainer_needed_bugs_only_counted(monkeypatch):
    async def search(api_key, **params):
        return [bug(1, "maintainer-needed@gentoo.org", "2026-01-01T00:00:00Z"),
                bug(2, "maintainer-needed@gentoo.org", "2026-01-02T00:00:00Z"),
                # Not counted, as the link shows only the bugs assigned to it
                bug(3, "larry@gentoo.org", "2026-01-03T00:00:00Z",
                    summary="app-misc/foo fails", cc=["maintainer-needed@gentoo.org"])]

    monkeypatch.setattr(bugzilla, "search", search)
    collector = BugsCollector(Config(gentoo_repo=Path("/nonexistent")))
    asyncio.run(collector.setup())

    maintainer = Maintainer("maintainer-needed@gentoo.org", packages=["app-misc/foo"])
    [section] = asyncio.run(collector.collect(maintainer))
    assert section.items == []
    assert section.omitted == "2 open bugs not shown"
    query = parse_qs(urlsplit(section.omitted_url).query)
    assert query["assigned_to"] == ["maintainer-needed@gentoo.org"]
    assert query["bug_status"] == ["UNCONFIRMED", "CONFIRMED", "IN_PROGRESS"]


def test_proxy_maint_bugs_only_counted(monkeypatch):
    async def search(api_key, **params):
        return [bug(1, "proxy-maint@gentoo.org", "2026-01-01T00:00:00Z",
                    cc=["proxy-maint@gentoo.org"]),
                bug(2, "larry@example.com", "2026-01-02T00:00:00Z",
                    cc=["proxy-maint@gentoo.org"]),
                bug(3, "larry@example.com", "2026-01-03T00:00:00Z")]

    monkeypatch.setattr(bugzilla, "search", search)
    collector = BugsCollector(Config(gentoo_repo=Path("/nonexistent")))
    asyncio.run(collector.setup())

    [section] = asyncio.run(collector.collect(Maintainer("proxy-maint@gentoo.org")))
    assert section.items == []
    assert section.omitted == "2 open bugs not shown"
    query = parse_qs(urlsplit(section.omitted_url).query)
    assert query["email1"] == ["proxy-maint@gentoo.org"]
    assert query["emailtype1"] == ["exact"]
    assert query["emailassigned_to1"] == query["emailcc1"] == ["1"]
    assert query["bug_status"] == ["UNCONFIRMED", "CONFIRMED", "IN_PROGRESS"]


@pytest.mark.parametrize("summary, packages", [
    ("dev-debug/rr-5.5.0 installs files", {"dev-debug/rr"}),
    ("dev-debug/rr: bundles blake2, app-arch/brotli", {"dev-debug/rr", "app-arch/brotli"}),
    (">=dev-debug/rr-5.9.0-r1 - QA Notice", {"dev-debug/rr"}),
    ("=dev-lang/python-3.12.1:3.12 fails", {"dev-lang/python"}),
    ("[guru] app-misc/foo-bar-2 fails", {"app-misc/foo-bar"}),
    ("please stabilize (x11-libs/libX11).", {"x11-libs/libX11"}),
    ("=dev-libs/foo-1.2* fails", {"dev-libs/foo"}),
    # Not versions, so part of the name
    ("media-fonts/font-adobe-100dpi: fails", {"media-fonts/font-adobe-100dpi"}),
    ("media-fonts/font-adobe-100dpi-1.0.4 fails", {"media-fonts/font-adobe-100dpi"}),
    ("dev-libs/foo[python] fails", {"dev-libs/foo"}),
    ("=dev-libs/foo-1.0[x] fails", {"dev-libs/foo"}),
    ('"app-misc/foo" fails', {"app-misc/foo"}),
    ("app-misc/foo's tests fail", {"app-misc/foo"}),
    ("app-misc/foo... broken", {"app-misc/foo"}),
    ("installs /usr/lib/foo", set()),
    ("app-misc/foo/files/bar fails", set()),
])
def test_summary_packages(summary, packages):
    assert summary_packages(summary) == packages


def test_open_bugs_of_co_maintained_packages(monkeypatch):
    async def search(api_key, **params):
        return [
            bug(1, "toolchain@gentoo.org", "2026-01-01T00:00:00Z",
                summary="dev-debug/rr-5.7.0 fails to compile", cc=["Lu_zero@gentoo.org"]),
            # CCs lu_zero, but not about one of their packages
            bug(2, "toolchain@gentoo.org", "2026-01-02T00:00:00Z",
                summary="sys-devel/gcc-15 fails", cc=["lu_zero@gentoo.org"]),
            bug(3, "lu_zero@gentoo.org", "2026-01-03T00:00:00Z",
                summary="media-video/ffmpeg: fails", cc=["lu_zero@gentoo.org"]),
            # About one of their packages, but does not CC them
            bug(4, "toolchain@gentoo.org", "2026-01-04T00:00:00Z",
                summary="dev-debug/rr: bundles blake2"),
        ]

    monkeypatch.setattr(bugzilla, "search", search)
    collector = BugsCollector(Config(gentoo_repo=Path("/nonexistent")))
    asyncio.run(collector.setup())

    maintainer = Maintainer("lu_zero@gentoo.org", packages=["dev-debug/rr", "media-video/ffmpeg"])
    [section] = asyncio.run(collector.collect(maintainer))
    assert [(item.ref, item.text) for item in section.items] == [
        ("Bug #1", ": dev-debug/rr-5.7.0 fails to compile "
                   "(last changed 2026-01-01, assigned to toolchain@gentoo.org)"),
        ("Bug #3", ": media-video/ffmpeg: fails (last changed 2026-01-03)"),
    ]


def test_proxy_maint_new_bugs_only_counted(monkeypatch):
    async def search(api_key, **params):
        if params["chfield"] == "[Bug creation]":
            return [bug(1, "larry@example.com", "2026-01-01T00:00:00Z",
                        cc=["proxy-maint@gentoo.org"]),
                    bug(2, "larry@example.com", "2026-01-02T00:00:00Z")]
        return [bug(3, "proxy-maint@gentoo.org", "2026-01-03T00:00:00Z")]

    monkeypatch.setattr(bugzilla, "search", search)
    collector = NewBugsCollector(Config(gentoo_repo=Path("/nonexistent")))
    asyncio.run(collector.setup())

    first, last = previous_month(datetime.now(UTC).date())
    new, closed = asyncio.run(collector.collect(Maintainer("proxy-maint@gentoo.org")))
    assert new.items == closed.items == []
    assert new.omitted == closed.omitted == "1 bug not shown"
    for section in new, closed:
        query = parse_qs(urlsplit(section.omitted_url).query)
        assert query["email1"] == ["proxy-maint@gentoo.org"]
        assert query["emailassigned_to1"] == query["emailcc1"] == ["1"]
        assert query["chfieldfrom"] == [first.isoformat()]
        assert query["chfieldto"] == [last.isoformat()]
    assert parse_qs(urlsplit(new.omitted_url).query)["chfield"] == ["[Bug creation]"]
    query = parse_qs(urlsplit(closed.omitted_url).query)
    assert query["bug_status"] == ["RESOLVED", "VERIFIED"]
    assert query["chfieldvalue"] == ["RESOLVED"]


def test_new_bugs_of_co_maintained_packages(monkeypatch):
    async def search(api_key, **params):
        return [bug(1, "toolchain@gentoo.org", "2026-01-01T00:00:00Z",
                    summary="dev-debug/rr: bundles blake2", cc=["lu_zero@gentoo.org"])]

    monkeypatch.setattr(bugzilla, "search", search)
    collector = NewBugsCollector(Config(gentoo_repo=Path("/nonexistent")))
    asyncio.run(collector.setup())

    maintainer = Maintainer("lu_zero@gentoo.org", packages=["dev-debug/rr"])
    new, closed = asyncio.run(collector.collect(maintainer))
    for section in new, closed:
        assert [item.text for item in section.items] == [
            ": dev-debug/rr: bundles blake2 (assigned to toolchain@gentoo.org)"]
