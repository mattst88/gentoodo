# SPDX-License-Identifier: GPL-3.0-or-later

"""Report on proxied maintainers, for their proxies: when and why each was
added to their packages, whether and when they last committed, and their
Bugzilla status."""

import argparse
import asyncio
import json
import logging
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

from . import __version__, repo
from .cli import positive_int
from .collectors.bugzilla import URL as BUGZILLA, rest as bugzilla
from .collectors.proxied import by_last_authored
from .config import Config, load_config
from .maintainers import Commit, Maintainer, _git, find_last_commits, load_maintainers, read_blobs

log = logging.getLogger(__name__)

# Bugs whose comments are fetched at a time, looking for a person's last one
COMMENT_BATCH = 20
# metadata.xml is read with regular expressions, not ElementTree, so that a
# version that does not parse still says who it lists.
COMMENT = re.compile(rb"<!--.*?-->", re.DOTALL)
MAINTAINER = re.compile(rb"<maintainer\b.*?</maintainer>", re.DOTALL)
UPSTREAM = re.compile(rb"<upstream\b.*?</upstream>", re.DOTALL)
EMAIL = re.compile(rb"<email>(.*?)</email>", re.DOTALL)
NAME = re.compile(rb"<name>(.*?)</name>", re.DOTALL)
# What CVS commits appended to their message
PORTAGE_VERSION = re.compile(r"\s*\(Portage version: [^)]*\)\s*$")
# Shortest name or local part, normalized, that says two addresses may be
# the same person's
MIN_MATCH = 4
# Local parts too common to say two addresses are the same person's
GENERIC_LOCAL = {"admin", "bugs", "contact", "dev", "gentoo", "git", "info", "mail", "me"}


@dataclass
class Change:
    """A commit that changed a maintainer's address."""
    old: str
    repo: str
    commit: str
    date: str
    subject: str


@dataclass
class Addition:
    """The commit that last added a maintainer to a package's metadata.xml,
    under the address `email`, and the commits that changed it since."""
    package: str
    email: str
    repo: str  # gentoo.git or historical.git
    commit: str
    date: str
    author: str
    subject: str
    changes: list[Change] = field(default_factory=list)


@dataclass
class Candidate:
    """Another address that may be the maintainer's, from commits under the
    same name or local part."""
    email: str
    names: list[str]
    commits: int
    last: str
    own_commits: int  # of those commits, the ones to the maintainer's packages


@dataclass
class Report:
    email: str
    name: str
    packages: list[str]
    last_authored: Commit | None = None
    additions: list[Addition] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    guru_packages: list[str] = field(default_factory=list)
    guru_last_authored: Commit | None = None
    tracker_bugs: list[dict] = field(default_factory=list)
    assigned_bugs: list[dict] = field(default_factory=list)
    last_comment: tuple[str, int] | None = None  # date, bug


class LogEntry(NamedTuple):
    """A commit changing a file, with no blob where it deleted the file."""
    commit: str
    date: str
    author: str
    subject: str
    blob: bytes | None


def _log_blobs(repo: Path, path: str) -> list[LogEntry]:
    """Each commit changing `path`, oldest first."""
    # A merge is diffed against its first parent: without that it has no
    # --raw line, and would look like it deleted the file.
    out = _git(repo, "log", "--reverse", "--no-renames", "--raw", "--no-abbrev",
               "--diff-merges=first-parent",
               "--format=%x01%H%x00%as%x00%aN%x00%s", "--", path).decode(errors="replace")
    entries = []
    for chunk in out.split("\x01")[1:]:
        header, _, raw = chunk.partition("\n")
        commit, date, author, subject = header.split("\0", 3)
        subject = PORTAGE_VERSION.sub("", subject)
        blob = None
        for line in raw.splitlines():
            if line.startswith(":"):
                oid = line.split("\t", 1)[0].split()[3]
                blob = None if set(oid) == {"0"} else oid.encode()
        entries.append(LogEntry(commit, date, author, subject, blob))
    return entries


async def find_additions(repo: Path, pairs: set[tuple[str, str]], label: str,
                         jobs: int) -> dict[tuple[str, str], Addition]:
    """For each (email, package), the commit in `repo` that last added the
    email to the package's metadata.xml, if any."""
    packages = sorted({package for _, package in pairs})
    semaphore = asyncio.Semaphore(jobs)

    async def history(package):
        async with semaphore:
            return package, await asyncio.to_thread(
                _log_blobs, repo, f"{package}/metadata.xml")

    histories = dict(await asyncio.gather(*(history(p) for p in packages)))
    oids = list(dict.fromkeys(
        e.blob for entries in histories.values() for e in entries if e.blob))
    log.info("Reading %d versions of %d metadata.xml files in %s",
             len(oids), len(packages), repo)
    blobs = await asyncio.to_thread(read_blobs, repo, oids)

    listed = {oid: _maintainers(data) for oid, data in blobs.items()}

    def has(email, entry):
        return entry.blob is not None and email in listed[entry.blob]

    def added(email, package, entries):
        """The addition of `email` in `entries`, following changes of address
        back to the one it was added under."""
        starts = [i for i, entry in enumerate(entries)
                  if has(email, entry) and (i == 0 or not has(email, entries[i - 1]))]
        if not starts:
            return None
        start = starts[-1]
        entry = entries[start]
        before = entries[start - 1].blob if start else None
        if before is not None:
            # The same name under an address removed here: a change of address
            norm_name = _norm(listed[entry.blob][email])
            old = [e for e, n in listed[before].items()
                   if e not in listed[entry.blob] and norm_name and _norm(n) == norm_name]
            if len(old) == 1 and (earlier := added(old[0], package, entries[:start])):
                earlier.changes.append(Change(old[0], label, entry.commit, entry.date,
                                              entry.subject))
                return earlier
        return Addition(package, email, label, entry.commit, entry.date, entry.author,
                        entry.subject)

    additions = {}
    for email, package in pairs:
        if addition := added(email, package, histories[package]):
            additions[email, package] = addition
    return additions


def _maintainers(data: bytes) -> dict[str, str]:
    """Map the email of each maintainer in a metadata.xml, not in <upstream>,
    to their name."""
    result = {}
    for block in MAINTAINER.findall(UPSTREAM.sub(b"", COMMENT.sub(b"", data))):
        if email := EMAIL.search(block):
            name = NAME.search(block)
            result[email[1].strip().lower().decode(errors="replace")] = (
                name[1].strip().decode(errors="replace") if name else "")
    return result


def _norm(name: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", name) if c.isalnum()).lower()


def find_candidates(repo: Path, reports: list[Report]) -> None:
    """Set `candidates` on each report: other authors with the maintainer's
    name or the local part of their address, who committed to their
    packages more recently than the maintainer did."""
    if not reports:
        return
    out = _git(repo, "log", "--format=%aE%x00%aN%x00%as%x00%s").decode(errors="replace")
    commits: Counter[str] = Counter()
    last: dict[str, str] = {}
    names: dict[str, set[str]] = defaultdict(set)
    by_package: dict[str, Counter[str]] = defaultdict(Counter)
    for line in out.splitlines():
        email, name, date, subject = line.split("\0", 3)
        email = email.lower()
        commits[email] += 1
        last.setdefault(email, date)
        names[email].add(name)
        by_package[email][subject.partition(":")[0].strip()] += 1

    by_name: dict[str, set[str]] = defaultdict(set)
    by_local: dict[str, set[str]] = defaultdict(set)
    for email, author_names in names.items():
        for name in author_names:
            by_name[_norm(name)].add(email)
        by_local[email.partition("@")[0].split("+")[0]].add(email)

    for report in reports:
        local = report.email.partition("@")[0].split("+")[0]
        found = set()
        if len(_norm(report.name)) >= MIN_MATCH:
            found |= by_name.get(_norm(report.name), set())
        if len(local) >= MIN_MATCH and local not in GENERIC_LOCAL:
            found |= by_local.get(local, set())
        for email in sorted(found - {report.email}):
            own = sum(by_package[email][p] for p in report.packages)
            if own and (report.last_authored is None
                        or last[email] > report.last_authored.day):
                report.candidates.append(Candidate(email, sorted(names[email]),
                                                   commits[email], last[email], own))


def bugzilla_status(api_key: str, report: Report) -> None:
    """Set the Bugzilla fields of `report`."""
    email = report.email
    person = {"email1": email, "emailtype1": "equals"}
    report.tracker_bugs = bugzilla(
        api_key, "bug", **person, emailassigned_to1=1, product="Gentoo Developers/Staff",
        component="Proxied Maintainers", include_fields="id,summary,status,resolution",
    )["bugs"]
    report.assigned_bugs = sorted(bugzilla(
        api_key, "bug", **person, emailassigned_to1=1, resolution="---",
        include_fields="id,summary,product,component,status,last_change_time",
    )["bugs"], key=lambda bug: bug["id"])

    # A bug changed no earlier than their last comment on it, so look at the
    # most recently changed first and stop once none can have a later one.
    commented = bugzilla(api_key, "bug", **person, emaillongdesc1=1,
                         include_fields="id,last_change_time",
                         order="changeddate DESC")["bugs"]
    best = None
    for i in range(0, len(commented), COMMENT_BATCH):
        batch = commented[i:i + COMMENT_BATCH]
        if best and max(bug["last_change_time"] for bug in batch) < best[0]:
            break
        ids = [bug["id"] for bug in batch]
        comments = bugzilla(api_key, f"bug/{ids[0]}/comment", ids=ids[1:])["bugs"]
        for bug, data in comments.items():
            for comment in data["comments"]:
                if (comment.get("creator") or "").lower() == email:
                    if best is None or comment["time"] > best[0]:
                        best = (comment["time"], int(bug))
    report.last_comment = (best[0][:10], best[1]) if best else None


async def audit(config: Config, emails: list[str], never: bool, jobs: int,
                proxy: str | None = None) -> list[Report]:
    """Report on the maintainers listed with proxied="yes", or only on those
    `proxy` is the proxy for, on those packages."""
    gentoo = config.gentoo_repo
    maintainers = load_maintainers(gentoo)
    if proxy is None:
        proxied = {email: m for email, m in maintainers.items() if m.proxied}
    else:
        if not (by := maintainers.get(proxy)) or not by.proxied_maintainers:
            sys.exit(f"{proxy} is not the proxy for any maintainers in {gentoo}")
        proxied = {email: replace(maintainers[email], packages=packages)
                   for email, packages in by.proxied_maintainers.items()}
    if emails:
        unknown = [e for e in emails if e not in proxied]
        if unknown:
            sys.exit(f"Not proxied maintainers{f' of {proxy}' if proxy else ''} "
                     f"in {gentoo}: {', '.join(unknown)}")
        proxied = {e: proxied[e] for e in emails}

    people = [replace(m, person=True)
              for m in proxied.values()]
    log.info("Finding the last commits of %d proxied maintainers", len(people))
    await asyncio.to_thread(find_last_commits, gentoo, people)
    if never:
        people = [m for m in people if m.last_authored is None]
    reports = [Report(m.email, m.name, m.packages, m.last_authored) for m in people]
    if not reports:
        return []

    for path in filter(None, (gentoo, config.historical_repo)):
        # Makes the per-package history fast: without changed-path Bloom
        # filters, each package's history reads all of it.
        log.info("Updating the commit-graph of %s", path)
        await asyncio.to_thread(_git, path, "commit-graph", "write", "--reachable",
                                "--changed-paths", "--split")

    pairs = {(r.email, p) for r in reports for p in r.packages}
    additions = await find_additions(gentoo, pairs, "gentoo.git", jobs)
    roots = set(_git(gentoo, "rev-list", "--max-parents=0", "HEAD").decode().split())
    # Those listed in gentoo.git's first commit were added in CVS.
    initial = {pair: a for pair, a in additions.items()
               if a.commit in roots}
    if initial and config.historical_repo:
        older = await find_additions(config.historical_repo,
                                     {(a.email, a.package) for a in initial.values()},
                                     "historical.git", jobs)
        for pair, a in initial.items():
            if earlier := older.get((a.email, a.package)):
                earlier.changes += a.changes
                additions[pair] = earlier
    for r in reports:
        r.additions = [additions[r.email, p] for p in r.packages if (r.email, p) in additions]

    await asyncio.to_thread(find_candidates, gentoo, reports)

    if config.guru_repo:
        guru = load_maintainers(config.guru_repo)
        guru_people = [replace(guru.get(r.email) or Maintainer(r.email), person=True)
                       for r in reports]
        await asyncio.to_thread(find_last_commits, config.guru_repo, guru_people)
        for r, m in zip(reports, guru_people):
            r.guru_packages = m.packages
            r.guru_last_authored = m.last_authored

    if config.bugzilla_api_key:
        log.info("Reading Bugzilla")
        semaphore = asyncio.Semaphore(jobs)

        async def status(report):
            async with semaphore:
                await asyncio.to_thread(bugzilla_status, config.bugzilla_api_key, report)

        await asyncio.gather(*(status(r) for r in reports))
    else:
        log.warning("No Bugzilla API key, set secret_file in [bugzilla]; "
                    "leaving out Bugzilla")

    reports.sort(key=lambda r: (by_last_authored(r.last_authored), r.email))
    return reports


def format_report(r: Report) -> str:
    lines = [f"{r.email}" + (f" ({r.name})" if r.name else "")]
    lines.append(f"  packages: {' '.join(r.packages)}")
    lines.append(f"  last authored: {r.last_authored or 'never'}")
    for a in r.additions:
        address = f" as {a.email}" if a.email != r.email else ""
        lines.append(f"  added to {a.package}{address}: {a.repo} {a.commit[:12]} {a.date} "
                     f"by {a.author} (\"{a.subject}\")")
        for c in a.changes:
            lines.append(f"    address changed from {c.old}: {c.repo} {c.commit[:12]} "
                         f"{c.date} (\"{c.subject}\")")
    if missing := [p for p in r.packages if p not in {a.package for a in r.additions}]:
        lines.append(f"  added to {' '.join(missing)}: not found")
    for c in r.candidates:
        lines.append(f"  maybe also {c.email} ({', '.join(c.names)}): {c.commits} commits, "
                     f"{c.own_commits} to their packages, last {c.last}")
    if r.guru_packages or r.guru_last_authored:
        lines.append(f"  GURU: {len(r.guru_packages)} packages, last authored "
                     f"{r.guru_last_authored or 'never'}")
    for bug in r.tracker_bugs:
        status = " ".join(filter(None, (bug["status"], bug.get("resolution"))))
        lines.append(f"  tracker bug: {BUGZILLA}/{bug['id']} {status}: {bug['summary']}")
    trackers = {bug["id"] for bug in r.tracker_bugs}
    if assigned := [bug for bug in r.assigned_bugs if bug["id"] not in trackers]:
        lines.append(f"  other open bugs assigned: {len(assigned)}")
        for bug in assigned:
            lines.append(f"    {BUGZILLA}/{bug['id']} {bug['status']} "
                         f"{bug['last_change_time'][:10]}: {bug['summary']}")
    if r.last_comment:
        lines.append(f"  last Bugzilla comment: {r.last_comment[0]} on "
                     f"{BUGZILLA}/{r.last_comment[1]}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gentoodo-proxied",
        description="Report on proxied maintainers: when and why each was added, "
                    "their last commits, and their Bugzilla status",
    )
    parser.add_argument("--config", required=True,
                        help="path to the TOML configuration file")
    parser.add_argument("emails", nargs="*", metavar="EMAIL",
                        help="only report on these proxied maintainers (default: all)")
    parser.add_argument("--proxy", metavar="EMAIL",
                        help="only report on the maintainers this developer or project "
                             "is the proxy for, on those packages")
    parser.add_argument("--never", action="store_true",
                        help="only report on those who never authored a commit")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--no-pull", action="store_true",
                        help="do not update the gentoo repository first")
    parser.add_argument("-j", "--jobs", type=positive_int, default=8,
                        help="git and Bugzilla requests to run concurrently "
                             "(default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="enable debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    try:
        config.gentoo_repo = await repo.update(config, not args.no_pull)
    except (repo.CloneError, RuntimeError) as e:
        sys.exit(f"Failed to update {repo.URL}: {e}")
    try:
        reports = await audit(config, [e.lower() for e in args.emails], args.never,
                              args.jobs,
                              args.proxy.lower() if args.proxy else None)
    except RuntimeError as e:
        sys.exit(str(e))
    if args.json:
        json.dump([asdict(r) for r in reports], sys.stdout, indent=2, ensure_ascii=False)
        print()
    else:
        print("\n\n".join(format_report(r) for r in reports))


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
        force=True,
    )
    asyncio.run(async_main(args))
