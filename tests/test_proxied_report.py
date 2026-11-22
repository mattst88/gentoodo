# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import os
import subprocess

from gentoodo import proxied_report
from gentoodo.proxied_report import Report, bugzilla_status, find_additions, find_candidates
from gentoodo.config import Config

from test_maintainers import METADATA, commit


def write(repo, package, body):
    path = repo / package / "metadata.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(METADATA.format(body))
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)


def person(email, name):
    return (f'<maintainer type="person" proxied="yes"><email>{email}</email>'
            f"<name>{name}</name></maintainer>")


def init(path):
    subprocess.run(["git", "init", "-q", path], check=True)
    return path


def test_find_additions(tmp_path, git_env):
    repo = init(tmp_path)
    write(repo, "app-misc/foo", person("old@example.com", "Some Body"))
    commit(repo, "app-misc/foo: add", "dev@gentoo.org", "dev@gentoo.org", "2020-01-01T00:00:00Z")
    write(repo, "app-misc/foo", person("new@example.com", "Some Body"))
    commit(repo, "app-misc/foo: update email", "dev@gentoo.org", "dev@gentoo.org",
           "2021-01-01T00:00:00Z")
    # Someone else taking over is not a change of address.
    write(repo, "app-misc/bar", person("before@example.com", "Before"))
    commit(repo, "app-misc/bar: add", "dev@gentoo.org", "dev@gentoo.org", "2020-01-01T00:00:00Z")
    write(repo, "app-misc/bar", person("after@example.com", "After"))
    commit(repo, "app-misc/bar: new maintainer", "dev@gentoo.org", "dev@gentoo.org",
           "2021-01-01T00:00:00Z")
    # Only the last time they were added counts.
    write(repo, "app-misc/baz", person("new@example.com", "Some Body"))
    commit(repo, "app-misc/baz: add", "dev@gentoo.org", "dev@gentoo.org", "2020-01-01T00:00:00Z")
    write(repo, "app-misc/baz", "<!-- maintainer-needed -->")
    commit(repo, "app-misc/baz: drop", "dev@gentoo.org", "dev@gentoo.org", "2021-01-01T00:00:00Z")
    write(repo, "app-misc/baz", person("NEW@example.com", "Some Body"))
    commit(repo, "app-misc/baz: take", "dev@gentoo.org", "dev@gentoo.org", "2022-01-01T00:00:00Z")
    # Being listed in <upstream> first is not being added.
    write(repo, "app-misc/qux", "<upstream><maintainer><email>up@example.com</email>"
          "</maintainer></upstream>")
    commit(repo, "app-misc/qux: add upstream", "dev@gentoo.org", "dev@gentoo.org",
           "2020-01-01T00:00:00Z")
    write(repo, "app-misc/qux", person("up@example.com", "Up") + "<upstream><maintainer>"
          "<email>up@example.com</email></maintainer></upstream>")
    commit(repo, "app-misc/qux: add maintainer", "dev@gentoo.org", "dev@gentoo.org",
           "2021-01-01T00:00:00Z")

    additions = asyncio.run(find_additions(repo, {
        ("new@example.com", "app-misc/foo"), ("after@example.com", "app-misc/bar"),
        ("new@example.com", "app-misc/baz"), ("absent@example.com", "app-misc/foo"),
        ("up@example.com", "app-misc/qux"),
    }, "gentoo.git", 2))

    foo = additions["new@example.com", "app-misc/foo"]
    assert (foo.email, foo.subject, foo.date) == ("old@example.com", "app-misc/foo: add",
                                                  "2020-01-01")
    [change] = foo.changes
    assert (change.old, change.subject) == ("old@example.com", "app-misc/foo: update email")
    bar = additions["after@example.com", "app-misc/bar"]
    assert bar.subject == "app-misc/bar: new maintainer" and not bar.changes
    assert additions["new@example.com", "app-misc/baz"].subject == "app-misc/baz: take"
    assert ("absent@example.com", "app-misc/foo") not in additions
    assert additions["up@example.com", "app-misc/qux"].subject == "app-misc/qux: add maintainer"


def test_audit_historical(tmp_path, git_env):
    historical = init(tmp_path / "historical")
    write(historical, "app-misc/foo", person("someone@example.com", "Some One"))
    commit(historical, "Initial import, ebuild by Some One", "dev@gentoo.org",
           "dev@gentoo.org", "2010-01-01T00:00:00Z")
    gentoo = init(tmp_path / "gentoo")
    write(gentoo, "app-misc/foo", person("someone@example.com", "Some One")
          + '<maintainer type="project"><email>proxy-maint@gentoo.org</email></maintainer>')
    write(gentoo, "app-misc/bar", '<maintainer type="person"><email>dev@gentoo.org</email>'
          "</maintainer><upstream><maintainer cc-on-bugs=\"yes\">"
          "<email>author@example.com</email></maintainer></upstream>")
    commit(gentoo, "proj/gentoo: Initial commit", "dev@gentoo.org", "dev@gentoo.org",
           "2015-08-08T00:00:00Z")

    config = Config(gentoo_repo=gentoo, historical_repo=historical)
    [report] = asyncio.run(proxied_report.audit(config, [], never=False, jobs=2))

    assert (report.email, report.name, report.last_authored) == (
        "someone@example.com", "Some One", None)
    [addition] = report.additions
    assert (addition.repo, addition.subject) == ("historical.git",
                                                 "Initial import, ebuild by Some One")


def test_audit_proxy(tmp_path, git_env):
    gentoo = init(tmp_path)
    write(gentoo, "app-misc/foo", person("someone@example.com", "Some One")
          + '<maintainer type="person" proxied="proxy"><email>dev@gentoo.org</email></maintainer>')
    write(gentoo, "app-misc/bar", person("someone@example.com", "Some One")
          + person("other@example.com", "Other")
          + '<maintainer type="project" proxied="proxy"><email>proxy-maint@gentoo.org</email>'
          + "</maintainer>")
    # Without proxied="yes", not a proxied maintainer
    write(gentoo, "app-misc/baz", '<maintainer type="person"><email>plain@example.com</email>'
          + "</maintainer>")
    commit(gentoo, "app-misc: add", "dev@gentoo.org", "dev@gentoo.org", "2020-01-01T00:00:00Z")
    config = Config(gentoo_repo=gentoo)

    reports = asyncio.run(proxied_report.audit(config, [], never=False, jobs=2))
    assert {(r.email, tuple(r.packages)) for r in reports} == {
        ("someone@example.com", ("app-misc/bar", "app-misc/foo")),
        ("other@example.com", ("app-misc/bar",)),
    }

    [report] = asyncio.run(proxied_report.audit(config, [], never=False, jobs=2, proxy="dev@gentoo.org"))
    assert (report.email, report.packages) == ("someone@example.com", ["app-misc/foo"])
    [addition] = report.additions
    assert addition.package == "app-misc/foo"


def test_find_candidates(tmp_path, git_env):
    repo = init(tmp_path)
    for subject, email in (("app-misc/foo: bump", "other@example.org"),
                           ("app-misc/unrelated: bump", "unrelated@example.org")):
        subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", subject],
                       check=True, env=os.environ | {
                           "GIT_AUTHOR_NAME": "Some Body", "GIT_AUTHOR_EMAIL": email,
                           "GIT_COMMITTER_NAME": "C", "GIT_COMMITTER_EMAIL": "c@gentoo.org",
                           "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z"})
    report = Report("someone@example.com", "Some Body", ["app-misc/foo"])

    find_candidates(repo, [report])

    # Both are by "Some Body", but only one committed to their package.
    [candidate] = report.candidates
    assert (candidate.email, candidate.commits, candidate.own_commits) == (
        "other@example.org", 1, 1)

    # Not a candidate if the maintainer committed more recently
    report = Report("someone@example.com", "Some Body", ["app-misc/foo"],
                    last_authored="2024-06-01T00:00:00Z newer")
    find_candidates(repo, [report])
    assert not report.candidates


def test_bugzilla_status(monkeypatch):
    fetched = []

    def bugzilla(api_key, path, **params):
        if path.endswith("/comment"):
            ids = [int(path.split("/")[1]), *params["ids"]]
            fetched.extend(ids)
            return {"bugs": {str(i): {"comments": [
                {"creator": "Someone@example.com" if i == 5 else "dev@gentoo.org",
                 "time": f"2026-0{i}-01T00:00:00Z"}]} for i in ids}}
        if params.get("component") == "Proxied Maintainers":
            return {"bugs": [{"id": 1, "summary": "Maintainer: Someone", "status": "CONFIRMED",
                              "resolution": ""}]}
        if params.get("emailassigned_to1"):
            return {"bugs": [{"id": 1}]}
        # Commented on, most recently changed first
        return {"bugs": [{"id": i, "last_change_time": f"2026-0{i}-02T00:00:00Z"}
                         for i in range(9, 0, -1)]}

    monkeypatch.setattr(proxied_report, "bugzilla", bugzilla)
    monkeypatch.setattr(proxied_report, "COMMENT_BATCH", 2)
    report = Report("someone@example.com", "Someone", [])

    bugzilla_status("key", report)

    assert [bug["id"] for bug in report.tracker_bugs] == [1]
    assert report.last_comment == ("2026-05-01", 5)
    # Bugs 3 and below changed before their comment on 5, so are not read.
    assert fetched == [9, 8, 7, 6, 5, 4]


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.name=A", "-c", "user.email=a@gentoo.org",
                    *args], check=True, capture_output=True)


def test_find_additions_through_merge(tmp_path, git_env):
    repo = init(tmp_path)
    write(repo, "app-misc/foo", person("someone@example.com", "Some One"))
    commit(repo, "app-misc/foo: add", "dev@gentoo.org", "dev@gentoo.org", "2020-01-01T00:00:00Z")
    git(repo, "checkout", "-q", "-b", "side")
    write(repo, "app-misc/foo", person("someone@example.com", "Some One") + "<!-- side -->")
    commit(repo, "app-misc/foo: side", "dev@gentoo.org", "dev@gentoo.org", "2020-02-01T00:00:00Z")
    git(repo, "checkout", "-q", "-")
    write(repo, "app-misc/foo", person("someone@example.com", "Some One") + "<!-- main -->")
    commit(repo, "app-misc/foo: main", "dev@gentoo.org", "dev@gentoo.org", "2020-02-01T00:00:00Z")
    # Unlike either parent, so shown by git log -- path
    git(repo, "merge", "-q", "--no-commit", "-s", "ours", "side")
    write(repo, "app-misc/foo", person("someone@example.com", "Some One") + "<!-- both -->")
    commit(repo, "Merge side", "dev@gentoo.org", "dev@gentoo.org", "2020-03-01T00:00:00Z")
    write(repo, "app-misc/foo", person("someone@example.com", "Some One") + "<!-- after -->")
    commit(repo, "app-misc/foo: after", "dev@gentoo.org", "dev@gentoo.org", "2020-04-01T00:00:00Z")

    additions = asyncio.run(find_additions(
        repo, {("someone@example.com", "app-misc/foo")}, "gentoo.git", 2))

    assert additions["someone@example.com", "app-misc/foo"].subject == "app-misc/foo: add"


def test_commented_out_maintainer_is_not_listed(tmp_path, git_env):
    repo = init(tmp_path)
    write(repo, "app-misc/foo", "<!-- " + person("someone@example.com", "Some One") + " -->")
    commit(repo, "app-misc/foo: add", "dev@gentoo.org", "dev@gentoo.org", "2020-01-01T00:00:00Z")
    write(repo, "app-misc/foo", person("someone@example.com", "Some One"))
    commit(repo, "app-misc/foo: take", "dev@gentoo.org", "dev@gentoo.org", "2021-01-01T00:00:00Z")

    additions = asyncio.run(find_additions(
        repo, {("someone@example.com", "app-misc/foo")}, "gentoo.git", 2))

    assert additions["someone@example.com", "app-misc/foo"].subject == "app-misc/foo: take"
