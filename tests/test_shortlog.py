# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

from gentoodo.collectors import shortlog
from gentoodo.collectors.shortlog import ShortlogCollector, parse_projects
from gentoodo.config import Config
from gentoodo.maintainers import Maintainer
from gentoodo.util import previous_month

PROJECTS = b"""<?xml version="1.0" encoding="UTF-8"?>
<projects>
  <project>
    <email>x11@gentoo.org</email>
    <member><email>Larry@gentoo.org</email></member>
    <member><email>adam@gentoo.org</email></member>
    <member><email>zoe@gentoo.org</email></member>
  </project>
</projects>
"""


def entry(author, email, subject, committer=None, committer_email=None):
    """A commit as git log formats it, committed by its author by default."""
    return "\0".join([author, email, committer or author, committer_email or email,
                      subject]) + "\n"


LOG = (
    entry("Larry the Cow", "larry@gentoo.org", "app-misc/foo: bar") +
    entry("Somebody", "somebody@example.com", "app-misc/foo: baz",
          "Larry the Cow", "larry@gentoo.org") +
    entry("Larry the Cow", "larry@gentoo.org", "app-misc/foo: qux")
)


def collect(monkeypatch, maintainer, month=None, log=LOG):
    commands = []

    async def run(*args):
        commands.append(args)
        return log

    monkeypatch.setattr(shortlog, "run", run)
    monkeypatch.setattr(shortlog, "fetch", lambda url: PROJECTS)
    collector = ShortlogCollector(Config(gentoo_repo=Path("/nonexistent")), month)
    asyncio.run(collector.setup())
    return commands, asyncio.run(collector.collect(maintainer))


def test_shortlog_for_previous_month(monkeypatch):
    maintainer = Maintainer("larry@gentoo.org", packages=["app-misc/foo"])
    commands, [section] = collect(monkeypatch, maintainer)

    first, last = previous_month(datetime.now(UTC).date())
    assert f"--since={first} 00:00:00 +0000" in commands[0]
    assert f"--until={last} 23:59:59 +0000" in commands[0]
    assert commands[0][-2:] == ("--", "app-misc/foo")
    assert section.title == f"Changes to Your Packages in {first:%B %Y}"
    assert section.text == (
        "Larry the Cow (2):\n"
        "      app-misc/foo: bar\n"
        "      app-misc/foo: qux\n"
        "\n"
        "Somebody (1):\n"
        "      app-misc/foo: baz"
    )


def test_shortlog_split_by_project_members(monkeypatch):
    maintainer = Maintainer("x11@gentoo.org", packages=["app-misc/foo"])
    _, [members, others, committed] = collect(monkeypatch, maintainer)

    month = previous_month(datetime.now(UTC).date())[0].strftime("%B %Y")
    assert members.title == f"Changes to Your Packages by Project Members in {month}"
    assert members.text == (
        "Larry the Cow (2):\n"
        "      app-misc/foo: bar\n"
        "      app-misc/foo: qux"
    )
    assert others.title == f"Changes to Your Packages by Others in {month}"
    assert others.text == "Somebody (1):\n      app-misc/foo: baz"
    assert committed.title == f"Others' Changes Committed by Project Members in {month}"
    assert committed.columns == ["Committer", "Commits"]
    assert committed.rows == [["Larry the Cow", "1"]]
    assert committed.notice


def test_shortlog_for_given_month(monkeypatch):
    maintainer = Maintainer("larry@gentoo.org", packages=["app-misc/foo"])
    month = (date(2026, 2, 1), date(2026, 2, 28))
    commands, [section] = collect(monkeypatch, maintainer, month)

    assert "--since=2026-02-01 00:00:00 +0000" in commands[0]
    assert "--until=2026-02-28 23:59:59 +0000" in commands[0]
    assert section.title == "Changes to Your Packages in February 2026"


def test_shortlog_committed_by_project_members(monkeypatch):
    larry = ("Larry the Cow", "larry@gentoo.org")
    log = (
        entry("Somebody", "somebody@example.com", "app-misc/foo: a", *larry) +
        entry("Else", "else@example.com", "app-misc/foo: b", "Mary", "mary@gentoo.org") +
        entry("Else", "else@example.com", "app-misc/foo: c", *larry) +
        entry("Else", "else@example.com", "app-misc/foo: d") +
        entry("Mary", "mary@gentoo.org", "app-misc/foo: e", *larry) +
        entry("Else", "else@example.com", "app-misc/foo: f", "Zoe", "zoe@gentoo.org") +
        entry("Else", "else@example.com", "app-misc/foo: g", "Adam", "adam@gentoo.org")
    )
    maintainer = Maintainer("x11@gentoo.org", packages=["app-misc/foo"])
    _, [_, _, committed] = collect(monkeypatch, maintainer, log=log)

    # Mary is not a member of x11, so what she commits is not counted, and
    # what she authors is a change by others. Ties are in order of name.
    assert committed.rows == [["Larry the Cow", "3"], ["Adam", "1"], ["Zoe", "1"]]


def test_parse_projects():
    projects = parse_projects(b"""<?xml version="1.0" encoding="UTF-8"?>
<projects>
  <project>
    <email>emacs@gentoo.org</email>
    <subproject ref="gnu-emacs@gentoo.org" inherit-members="1"/>
    <subproject ref="xemacs@gentoo.org"/>
    <member><email>Larry@gentoo.org</email></member>
  </project>
  <project>
    <email>gnu-emacs@gentoo.org</email>
    <subproject ref="emacs@gentoo.org" inherit-members="1"/>
    <member><email>ulm@gentoo.org</email></member>
  </project>
  <project>
    <email>xemacs@gentoo.org</email>
    <member><email>xemacs-dev@gentoo.org</email></member>
  </project>
</projects>
""")

    assert projects["emacs@gentoo.org"] == {"larry@gentoo.org", "ulm@gentoo.org"}
    assert projects["gnu-emacs@gentoo.org"] == {"larry@gentoo.org", "ulm@gentoo.org"}
    assert projects["xemacs@gentoo.org"] == {"xemacs-dev@gentoo.org"}


def test_shortlog_arch_commits_only_counted(monkeypatch):
    log = (
        entry("Larry the Cow", "larry@gentoo.org", "app-misc/foo: bar") +
        entry("Arch Tester", "arch@gentoo.org", "app-misc/foo: Stabilize 1.0 amd64, #123") +
        entry("Arch Tester", "arch@gentoo.org", "app-misc/foo: Keyword 1.0 ~riscv, #124") +
        entry("Somebody", "somebody@example.com", "app-misc/foo: stabilize 1.0 for ALLARCHES") +
        entry("Somebody", "somebody@example.com", "app-misc/foo: keyword 1.0 for ~arm64") +
        entry("Somebody", "somebody@example.com", "app-misc/foo: Stabilize on amd64 is broken") +
        entry("Somebody", "somebody@example.com", "app-misc/foo: unkeyword 1.0 for ~x86")
    )
    maintainer = Maintainer("x11@gentoo.org", packages=["app-misc/foo"])
    _, [members, others, committed] = collect(monkeypatch, maintainer, log=log)

    assert members.text == "Larry the Cow (1):\n      app-misc/foo: bar"
    assert members.omitted == ""
    assert others.text == (
        "Somebody (2):\n"
        "      app-misc/foo: Stabilize on amd64 is broken\n"
        "      app-misc/foo: unkeyword 1.0 for ~x86"
    )
    assert others.omitted == "4 stabilization and keywording commits not shown"
    assert committed.rows == []


def test_shortlog_only_arch_commits(monkeypatch):
    log = entry("Arch Tester", "arch@gentoo.org", "app-misc/foo: Stabilize 1.0 amd64, #123")
    maintainer = Maintainer("larry@gentoo.org", packages=["app-misc/foo"])
    _, [section] = collect(monkeypatch, maintainer, log=log)

    assert section.text == ""
    assert section.omitted == "1 stabilization and keywording commit not shown"
