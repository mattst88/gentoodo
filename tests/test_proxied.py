# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import subprocess

from gentoodo.collectors.proxied import (ProxiedMaintainersCollector, by_last_authored,
                                         with_proxied)
from gentoodo.config import Config
from gentoodo.maintainers import Commit, find_last_commits, load_maintainers

from test_maintainers import add_package, commit


def test_proxied_maintainers(tmp_path, git_env):
    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    add_package(tmp_path, "app-misc/foo", """
        <maintainer type="person" proxied="yes"><email>old@example.com</email></maintainer>
        <maintainer type="person" proxied="yes"><email>new@example.org</email></maintainer>
        <maintainer type="person" proxied="yes"><email>idle@example.net</email></maintainer>
        <maintainer type="project" proxied="proxy"><email>proxy-maint@gentoo.org</email></maintainer>""")
    # A developer who maintains the package and is the proxy for it
    add_package(tmp_path, "app-misc/bar", """
        <maintainer type="person" proxied="yes"><email>new@example.org</email></maintainer>
        <maintainer type="person" proxied="maintainer-and-proxy"><email>dev@gentoo.org</email></maintainer>""")
    add_package(tmp_path, "app-misc/baz", """
        <maintainer type="person"><email>dev@gentoo.org</email></maintainer>
        <maintainer type="person"><email>other@gentoo.org</email></maintainer>""")
    commit(tmp_path, "app-misc/foo: add", "old@example.com", "dev@gentoo.org",
           "2020-01-01T00:00:00Z")
    commit(tmp_path, "app-misc/foo: bump", "new@example.org", "dev@gentoo.org",
           "2026-01-01T00:00:00Z")
    commit(tmp_path, "app-misc/bar: bump", "dev@gentoo.org", "dev@gentoo.org",
           "2026-02-01T00:00:00Z")

    maintainers = load_maintainers(tmp_path)
    proxy = maintainers["proxy-maint@gentoo.org"]
    # As with --maintainer proxy-maint@gentoo.org
    people = with_proxied([proxy], maintainers)
    assert [m.email for m in people] == [
        "proxy-maint@gentoo.org", "idle@example.net", "new@example.org", "old@example.com"]
    find_last_commits(tmp_path, people)
    collector = ProxiedMaintainersCollector(Config(gentoo_repo=tmp_path))
    collector.maintainers = maintainers

    async def collect(email):
        return await collector.collect(maintainers[email])

    assert asyncio.run(collect("other@gentoo.org")) == []
    [section] = asyncio.run(collect("proxy-maint@gentoo.org"))
    assert section.columns == ["Last authored", "Maintainer", "Commit"]
    assert section.rows == [
        ["never", "idle@example.net", ""],
        ["2020-01-01", "old@example.com", "app-misc/foo: add"],
        ["2026-01-01", "new@example.org", "app-misc/foo: bump"],
    ]
    [section] = asyncio.run(collect("dev@gentoo.org"))
    assert section.rows == [["2026-01-01", "new@example.org", "app-misc/foo: bump"]]


def test_by_last_authored_orders_across_time_zones():
    west = Commit("2026-01-01T01:00:00-05:00", "1" * 40, "later")
    east = Commit("2026-01-01T09:00:00+10:00", "2" * 40, "earlier")
    # As strings, west sorts first, though it is the later of the two.
    assert sorted([west, None, east], key=by_last_authored) == [None, east, west]
