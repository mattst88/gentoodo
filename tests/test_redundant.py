# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from pathlib import Path

from gentoodo.collectors import redundant
from gentoodo.collectors.base import package_link
from gentoodo.collectors.redundant import COLUMNS, RedundantVersionsCollector, consolidate
from gentoodo.config import Config
from gentoodo.maintainers import Maintainer


def test_redundant_versions(monkeypatch):
    async def report(cache_dir):
        return [
            {"category": "dev-lang", "package": "perl", "version": "5.40.2",
             "class": "RedundantVersion",
             "msg": "slot(0) keywords are overshadowed by version: 5.42.2"},
            {"category": "dev-lang", "package": "perl", "version": "5.42.2",
             "class": "VariableScope",
             "msg": "variable 'EROOT' used in 'src_configure' on line: 670"},
        ]

    monkeypatch.setattr(redundant.ci, "report", report)
    collector = RedundantVersionsCollector(Config(gentoo_repo=Path("/nonexistent")))
    asyncio.run(collector.setup())
    [section] = asyncio.run(collector.collect(Maintainer("perl@gentoo.org",
                                                        packages=["dev-lang/perl"])))

    assert section.columns == COLUMNS
    assert section.rows == [[package_link("dev-lang/perl"), "5.40.2", "5.42.2"]]


def test_consolidate_under_one_version():
    """The versions are grouped under 12.6.0, which overshadows all of
    them, not 12.2.0 or 12.7.0, which overshadow fewer."""
    assert consolidate("app-emulation/libvirt", {
        "11.10.0": {"12.0.0", "12.2.0", "12.6.0"},
        "12.0.0": {"12.2.0", "12.6.0"},
        "12.1.0-r1": {"12.2.0", "12.6.0", "12.7.0"},
        "12.2.0": {"12.6.0"},
    }) == [[package_link("app-emulation/libvirt"),
            "11.10.0, 12.0.0, 12.1.0-r1, 12.2.0", "12.6.0"]]


def test_consolidate_several_rows():
    """Versions no single version overshadows, as in different slots, are
    in rows of their own, by overshadowing version."""
    assert consolidate("dev-lang/python", {
        "3.13.1": {"3.13.2"},
        "3.12.8": {"3.12.9", "3.12.10"},
        "3.12.9": {"3.12.10"},
    }) == [[package_link("dev-lang/python"), "3.12.8, 3.12.9", "3.12.10"],
           [package_link("dev-lang/python"), "3.13.1", "3.13.2"]]
