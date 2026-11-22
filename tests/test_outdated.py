# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from pathlib import Path

import pytest

from gentoodo.collectors import outdated
from gentoodo.collectors.base import Link, package_link
from gentoodo.collectors.outdated import (
    COLUMNS, PR_COLUMN, OutdatedCollector, bumps, ebuild, parse_feed)
from gentoodo.collectors.pull_requests import PullRequest
from gentoodo.config import Config
from gentoodo.maintainers import Maintainer

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Outdated Packages for X11 &lt;x11@gentoo.org&gt;</title>
  <entry>
    <title>dev-libs/libevdev</title>
    <summary type="html">Version 1.14.0 is available, while the latest version in the Gentoo tree is 1.13.7.</summary>
  </entry>
  <entry>
    <title>x11-libs/libdrm</title>
    <summary type="html">Something else.</summary>
  </entry>
</feed>
"""


def test_parse_feed():
    assert parse_feed(FEED) == [
        [package_link("dev-libs/libevdev"), "1.13.7", "1.14.0"],
        [package_link("x11-libs/libdrm"), "Something else.", ""],
    ]


@pytest.mark.parametrize("path, expected", [
    ("dev-libs/libevdev/libevdev-1.14.0.ebuild", ("dev-libs/libevdev", "1.14.0")),
    ("dev-libs/libevdev/libevdev-1.14.0-r1.ebuild", ("dev-libs/libevdev", "1.14.0")),
    ("dev-libs/libevdev/Manifest", None),
    ("dev-libs/libevdev/files/libevdev-1.14.0.ebuild", None),
    # Not the package's own ebuild
    ("dev-libs/libevdev/other-1.14.0.ebuild", None),
    ("dev-libs/libevdev/libevdev.ebuild", None),
])
def test_ebuild(path, expected):
    cpv = ebuild(path)
    assert (cpv and (cpv.key, cpv.version)) == expected


def link(number):
    return Link(f"GitHub #{number}", f"https://example.org/{number}")


def pr(number, *added):
    return PullRequest(number, "Bump", f"https://example.org/{number}",
                       [*added, "dev-libs/libevdev/Manifest"], list(added))


def test_bumps():
    added = [("1.13.7", link(1)), ("1.13.8", link(2)), ("1.14.0", link(2)),
             ("1.13.6", link(3))]
    # Each once, and not those adding only a revision bump or older version
    assert bumps("dev-libs/libevdev", "1.13.7", added) == [link(2)]
    # Revisions of the latest in the tree do not count
    assert bumps("dev-libs/libevdev", "1.13.7-r2", added) == [link(2)]
    # Not versions at all
    assert bumps("dev-libs/libevdev", "Something else.", added) == []


def collect(monkeypatch, *forges):
    monkeypatch.setattr(outdated, "fetch", lambda url: FEED)
    collector = OutdatedCollector(Config(gentoo_repo=Path("/nonexistent")))
    asyncio.run(collector.setup())
    for forge, prs in forges:
        collector.add_pull_requests(forge, prs)
    [section] = asyncio.run(collector.collect(Maintainer("x11@gentoo.org")))
    return section


def test_bump_column(monkeypatch):
    section = collect(
        monkeypatch,
        ("GitHub", [pr(1, "dev-libs/libevdev/libevdev-1.14.0.ebuild")]),
        ("Codeberg", [pr(7, "dev-libs/libevdev/libevdev-1.13.9.ebuild")]))

    assert section.columns == [*COLUMNS, PR_COLUMN]
    assert [row[3] for row in section.rows] == [
        [link(1), Link("Codeberg #7", "https://example.org/7")], []]


def test_no_bump_column_without_forges(monkeypatch):
    section = collect(monkeypatch)
    assert section.columns == COLUMNS
    assert all(len(row) == 3 for row in section.rows)


def test_live_ebuilds_do_not_bump(monkeypatch):
    section = collect(monkeypatch, ("GitHub", [
        pr(1, "dev-libs/libevdev/libevdev-9999.ebuild"),
        pr(2, "dev-libs/libevdev/libevdev-1.9999.ebuild")]))

    assert [row[3] for row in section.rows] == [[], []]
