# SPDX-License-Identifier: GPL-3.0-or-later

from collections import defaultdict
from functools import partial

from .base import Cell, PackageTableCollector, package_link, package_version
from .. import ci

COLUMNS = ["Package", "Overshadowed versions", "Overshadowing version"]


def consolidate(package: str, redundant: dict[str, set[str]]) -> list[list[Cell]]:
    """Rows of `package`'s redundant versions, each mapped to the versions
    that overshadow it, grouped under few overshadowing versions.

    Each row's overshadowing version is the one that overshadows the most
    of the versions not yet in a row, preferring the newest.
    """
    key = partial(package_version, package)
    remaining = dict(redundant)
    rows = []
    while remaining:
        candidates = set().union(*remaining.values())
        best = max(candidates, key=lambda c: (
            sum(c in later for later in remaining.values()), key(c)))
        covered = sorted((v for v, later in remaining.items() if best in later), key=key)
        rows.append((key(best), [package_link(package), ", ".join(covered), best]))
        for v in covered:
            del remaining[v]
    return [row for _, row in sorted(rows, key=lambda r: r[0])]


def overshadowing(msg: str) -> set[str]:
    """The versions in a RedundantVersion message such as "slot(0) keywords
    are overshadowed by versions: 12.2.0, 12.6.0"."""
    return set(msg.rpartition(": ")[2].split(", "))


class RedundantVersionsCollector(PackageTableCollector):
    """pkgcheck's RedundantVersion results in the latest CI report, as a
    table of the versions overshadowed by each later version."""
    title = "Redundant Versions"
    columns = COLUMNS

    async def setup(self) -> None:
        results = await ci.report(self.config.cache_dir)
        redundant: dict[str, dict[str, set[str]]] = defaultdict(dict)
        for r in results:
            if r["class"] == "RedundantVersion":
                redundant[ci.package_of(r)][r["version"]] = overshadowing(r["msg"])
        self.rows = {package: consolidate(package, versions)
                     for package, versions in redundant.items()}
