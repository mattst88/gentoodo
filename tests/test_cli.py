# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import asyncio
from datetime import date
from pathlib import Path

import pytest

from gentoodo import __version__, cli, data
from gentoodo.cli import collector_names, link_pull_requests, month, parse_args, process
from gentoodo.collectors.base import Collector, Item, Link, Section
from gentoodo.collectors.outdated import OutdatedCollector
from gentoodo.collectors.pull_requests import PullRequest, PullRequestsCollector
from gentoodo.config import Config
from gentoodo.maintainers import Commit, Maintainer


class FakeCollector(Collector):
    def __init__(self, sections):
        self.sections = sections

    async def collect(self, maintainer):
        return self.sections


def written(tmp_path, *sections):
    config = Config(gentoo_repo=Path("/nonexistent"), output_dir=tmp_path)
    maintainer = Maintainer("x11@gentoo.org", packages=["x11-libs/libdrm"])
    asyncio.run(process(config, maintainer, {"fake": FakeCollector(list(sections))},
                        False, asyncio.Semaphore(1)))
    return list(tmp_path.glob("*.eml"))


NOTICE = Section("Open GitHub Pull Requests", text="Could not be fetched from GitHub.",
                 notice=True)


def test_notice_alone_sends_no_email(tmp_path):
    assert not written(tmp_path, NOTICE)


def test_notice_is_shown_with_other_sections(tmp_path):
    [email] = written(tmp_path, NOTICE, Section("Open Bugs", [Item("Bug #1")]))
    assert b"Could not be fetched from GitHub." in email.read_bytes()


ARCH_ONLY = Section("Bugs Filed in October 2026",
                    omitted="3 stabilization and keywording bugs not shown")


def test_arch_activity_alone_sends_no_email(tmp_path):
    assert not written(tmp_path, ARCH_ONLY)


def test_arch_activity_is_counted_with_other_sections(tmp_path):
    [email] = written(tmp_path, ARCH_ONLY, Section("Open Bugs", [Item("Bug #1")]))
    assert b"3 stabilization and keywording bugs not shown" in email.read_bytes()


def test_table_alone_sends_email(tmp_path):
    assert written(tmp_path, Section("Proxied Maintainers", rows=[["never", "a@b.org", ""]]))


def test_collector_names_in_email_order():
    assert collector_names("shortlog,github_prs,shortlog") == ["github_prs", "shortlog"]


def test_month():
    assert month("2026-10") == (date(2026, 10, 1), date(2026, 10, 31))
    for value in ("2026-13", "October", "2026-10-01"):
        with pytest.raises(argparse.ArgumentTypeError):
            month(value)


def test_send_outbox_maintainer(tmp_path, monkeypatch):
    config = Config(gentoo_repo=Path("/nonexistent"), output_dir=tmp_path)
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "send_outbox", lambda *args: calls.append(args) or 0)

    cli.main(["--config", "x", "--send-outbox", "--maintainer", "larry@gentoo.org"])
    assert calls == [(tmp_path, config.sendmail, False, "larry@gentoo.org")]


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        parse_args(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out == f"gentoodo {__version__}\n"


def test_process_returns_sections_by_collector(tmp_path):
    config = Config(gentoo_repo=Path("/nonexistent"), output_dir=tmp_path)
    bugs, prs = Section("Open Bugs", [Item("Bug #1")]), Section("Open GitHub Pull Requests")
    sections = asyncio.run(process(
        config, Maintainer("x11@gentoo.org"),
        {"bugs": FakeCollector([bugs]), "github_prs": FakeCollector([prs])},
        False, asyncio.Semaphore(1)))
    # Empty sections too, to decide again when made from saved data
    assert sections == [("bugs", bugs), ("github_prs", prs)]


BUGS = Section("Open Bugs", [Item("Bug #1", ": crash", "https://bugs.gentoo.org/1")])
QA = Section("New QA Warnings in November 2026", text="app-misc/foo: Warning")
REDUNDANT = Section("Redundant Versions", [Item("app-misc/foo-1")])
OUTDATED = Section("Outdated Packages", columns=["Package", "Bump PR"],
                   rows=[[Link("app-misc/foo", "https://x.org/foo"), [Link("PR #2", "https://x.org/2")]],
                         [Link("app-misc/bar", "https://x.org/bar"), []]])
PROXIED = Section("Proxied Maintainers", columns=["When", "Who"], rows=[["never", "a@b.org"]])


AUTHORED = Commit("2026-11-02T10:00:00+01:00", "0123456789abcdef0123456789abcdef01234567",
                  "x11-libs/libdrm: add 2.4.130")


def save(tmp_path):
    path = tmp_path / "data" / "november.json"
    # Not in the order of COLLECTORS
    data.save(path, [
        (Maintainer("x11@gentoo.org", packages=["x11-libs/libdrm"], last_authored=AUTHORED),
         [("redundant", REDUNDANT), ("qa", QA), ("bugs", BUGS), ("outdated", OUTDATED),
          ("proxied", PROXIED)]),
        (Maintainer("larry@gentoo.org"), [("bugs", Section("Open Bugs"))]),
    ])
    return path


def test_data_round_trip(tmp_path):
    path = save(tmp_path)

    [(x11, sections), (larry, _)] = data.load(path)

    assert x11 == Maintainer("x11@gentoo.org", packages=["x11-libs/libdrm"],
                             last_authored=AUTHORED)
    assert sections == [("redundant", REDUNDANT), ("qa", QA), ("bugs", BUGS),
                        ("outdated", OUTDATED), ("proxied", PROXIED)]
    assert larry.email == "larry@gentoo.org"


@pytest.mark.parametrize("text", ["", "[]", '{"format": 1}', '{"format": 2}',
                                  '{"format": 2, "maintainers": [{"maintainer": {}}]}'])
def test_data_load_invalid(tmp_path, text):
    (tmp_path / "x.json").write_text(text)
    with pytest.raises(ValueError):
        data.load(tmp_path / "x.json")


def from_data(tmp_path, monkeypatch, *args):
    path = save(tmp_path)
    outbox = tmp_path / "outbox"
    config = Config(gentoo_repo=Path("/nonexistent"), output_dir=outbox, multipart=False)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    # Nothing is queried, not even the repository
    monkeypatch.setattr(cli.repo, "update", None)
    monkeypatch.setattr(cli, "load_maintainers", None)
    cli.main(["--config", "x", "--from-data", str(path), *args])
    return {p.name.partition("_2")[0]: p.read_text() for p in outbox.glob("*.eml")}


def test_from_data(tmp_path, monkeypatch):
    emails = from_data(tmp_path, monkeypatch)

    # Nothing to report for larry@gentoo.org
    assert list(emails) == ["x11_at_gentoo.org"]
    text = emails["x11_at_gentoo.org"]
    # In the order of COLLECTORS now
    assert (text.index("New QA Warnings") < text.index("Redundant Versions")
            < text.index("Open Bugs"))
    assert ("Last commit authored: 2026-11-02T10:00:00+01:00 0123456789ab "
            "x11-libs/libdrm: add 2.4.130\n") in text


def test_from_data_collectors(tmp_path, monkeypatch):
    text = from_data(tmp_path, monkeypatch, "--collectors", "qa,bugs")["x11_at_gentoo.org"]
    assert "Open Bugs" in text
    assert "Redundant Versions" not in text


def test_from_data_maintainer(tmp_path, monkeypatch):
    assert from_data(tmp_path, monkeypatch, "--maintainer", "X11@gentoo.org")
    with pytest.raises(SystemExit, match="larry@example.org is not in"):
        from_data(tmp_path, monkeypatch, "--maintainer", "larry@example.org")


@pytest.mark.parametrize("args", [
    ["--from-data", "x.json", "--month", "2026-10"],
    ["--from-data", "x.json", "--save-data", "y.json"],
    ["--send-outbox", "--save-data", "y.json"],
])
def test_data_options_conflict(args):
    with pytest.raises(SystemExit) as e:
        parse_args(["--config", "x", *args])
    assert e.value.code == 2


def test_link_pull_requests():
    config = Config(gentoo_repo=Path("/nonexistent"))
    github, codeberg = PullRequestsCollector(config), PullRequestsCollector(config)
    github.forge, codeberg.forge = "GitHub", "Codeberg"
    github.prs = [PullRequest(1, "Bump", "https://x.org/1", [], ["a/b/b-2.ebuild"])]
    codeberg.prs = None  # could not be read
    outdated = OutdatedCollector(config)
    link_pull_requests({"github": github, "codeberg": codeberg, "outdated": outdated})

    assert outdated.added == {"a/b": [("2", Link("GitHub #1", "https://x.org/1"))]}
