# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from functools import partial
from urllib.error import HTTPError
from urllib.parse import quote

from pkgcore.ebuild.cpv import VersionedCPV
from pkgcore.ebuild.errors import InvalidCPV

from .base import Cell, Collector, Link, Section, package_link, package_version
from .pull_requests import PullRequest
from ..maintainers import Maintainer
from ..util import fetch

log = logging.getLogger(__name__)

URL = "https://packages.gentoo.org"
ATOM = "{http://www.w3.org/2005/Atom}"
SUMMARY_RE = re.compile(
    r"Version (.+?) is available, while the latest version in the Gentoo tree is (.+)\.")
COLUMNS = ["Package", "Gentoo version", "Upstream version"]
PR_COLUMN = "Bump PR"
# A live ebuild's version, such as 9999 or 2.9999
LIVE = re.compile(r"(^|\.)9999+$")


def ebuild(path: str) -> VersionedCPV | None:
    """The package and version of the ebuild at `path`, such as
    app-misc/foo/foo-1.2.ebuild, if it is one."""
    parts = path.split("/")
    if len(parts) != 3 or not parts[2].endswith(".ebuild"):
        return None
    category, name, filename = parts
    try:
        cpv = VersionedCPV(f"{category}/{filename.removesuffix('.ebuild')}")
    except InvalidCPV:
        return None
    return cpv if cpv.package == name else None


def bumps(package: str, version: str, added: list[tuple[str, Link]]) -> list[Link]:
    """The pull requests adding a version of `package` newer than `version`,
    the latest in the tree. Revisions are ignored, so a revision bump is not
    a bump."""
    key = partial(package_version, package)
    try:
        latest = key(key(version).version)
    except InvalidCPV:
        return []
    return list(dict.fromkeys(link for v, link in added if key(v) > latest))


def parse_feed(data: bytes) -> list[list[Cell]]:
    """A row of each package in the feed, with the summary in place of the
    versions where it does not give them."""
    rows = []
    for entry in ET.fromstring(data).iter(f"{ATOM}entry"):
        package = entry.findtext(f"{ATOM}title", "")
        summary = entry.findtext(f"{ATOM}summary", "")
        if match := SUMMARY_RE.search(summary):
            rows.append([package_link(package), match[2], match[1]])
        else:
            rows.append([package_link(package), summary, ""])
    return rows


class OutdatedCollector(Collector):
    # Versions added by open pull requests, by package, with a link to each.
    # None until add_pull_requests() is called, and without it the table has
    # no Bump PR column.
    added: dict[str, list[tuple[str, Link]]] | None = None

    def add_pull_requests(self, forge: str, prs: list[PullRequest]) -> None:
        """Record the ebuilds, other than live ones, that `prs` on `forge` add."""
        if self.added is None:
            self.added = defaultdict(list)
        for pr in prs:
            link = Link(f"{forge} #{pr.number}", pr.url)
            for path in pr.added:
                if (cpv := ebuild(path)) and not LIVE.search(cpv.version):
                    self.added[cpv.key].append((cpv.version, link))

    async def setup(self) -> None:
        self.not_found: list[str] = []

    async def collect(self, maintainer: Maintainer) -> list[Section]:
        url = f"{URL}/maintainer/{quote(maintainer.email)}/outdated.atom"
        try:
            data = await asyncio.to_thread(fetch, url)
        except HTTPError as e:
            if e.code != 404:
                raise
            self.not_found.append(maintainer.email)
            return []
        rows = parse_feed(data)
        if self.added is None:
            return [Section("Outdated Packages", columns=COLUMNS, rows=rows)]
        for row in rows:
            package = row[0].text
            row.append(bumps(package, row[1], self.added.get(package, [])))
        return [Section("Outdated Packages", columns=[*COLUMNS, PR_COLUMN], rows=rows)]

    def finish(self) -> None:
        # The site does not know these maintainers, so they may no longer
        # be valid.
        if self.not_found:
            log.warning("No outdated feed for %d maintainers: %s",
                        len(self.not_found), ", ".join(sorted(self.not_found)))
