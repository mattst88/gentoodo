# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import json
import re
from collections import defaultdict
from collections.abc import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode

from .base import Collector, Item, Section, omitted_text
from ..maintainers import MAINTAINER_NEEDED, PROXY_MAINT, Maintainer
from ..util import fetch

URL = "https://bugs.gentoo.org"
# The server returns up to 10000 bugs per request, but with their CCs takes
# over a minute for that many and may drop the connection.
PAGE_SIZE = 2000
FIELDS = "id,summary,component,assigned_to,cc,last_change_time"
ARCH_COMPONENTS = {"Stabilization", "Keywording"}
PRODUCT = "Gentoo Linux"  # package bugs
OPEN = ["UNCONFIRMED", "CONFIRMED", "IN_PROGRESS"]
CLOSED = ["RESOLVED", "VERIFIED"]
# The buglist.cgi parameters selecting the bugs assigned to or CCing proxy-maint
PROXY_MAINT_QUERY = {"email1": PROXY_MAINT, "emailtype1": "exact",
                     "emailassigned_to1": "1", "emailcc1": "1"}
# A package named in a bug summary, such as dev-debug/rr in "dev-debug/rr: ..."
# or ">=dev-debug/rr-5.7.0 fails to compile", without its version. As in
# portage.versions, the name is non-greedy and ends only before a version, so
# media-fonts/font-adobe-100dpi keeps its -100dpi.
VERSION = r"\d+(?:\.\d+)*[a-z]?(?:_(?:pre|p|beta|alpha|rc)\d*)*(?:-r\d+)?"
PACKAGE = re.compile(rf"(?<![\w+./-])[<>=~]*(\w[\w+.-]*/\w[\w+-]*?)(?:-{VERSION}\*?)?"
                     r"(?![\w+/-]|\.\w)", re.ASCII)


def rest(api_key: str, path: str, **params) -> dict:
    """GET `path` from the REST API, raising RuntimeError with Bugzilla's
    message if it gives one."""
    # bugs.gentoo.org ignores the X-BUGZILLA-API-KEY header.
    query = urlencode({**params, "Bugzilla_api_key": api_key}, doseq=True)
    try:
        return json.loads(fetch(f"{URL}/rest/{path}?{query}"))
    except HTTPError as e:
        try:
            message = json.loads(e.read())["message"]
        except (ValueError, KeyError):
            raise e from None
        raise RuntimeError(f"Bugzilla: {message}") from None


async def search(api_key: str | None, **params: str | list[str]) -> list[dict]:
    if not api_key:
        raise RuntimeError("no Bugzilla API key, set secret_file in [bugzilla]")

    bugs: list[dict] = []
    while True:
        page = (await asyncio.to_thread(
            rest, api_key, "bug", **params, product=PRODUCT, include_fields=FIELDS,
            order="bug_id", limit=PAGE_SIZE, offset=len(bugs)))["bugs"]
        bugs += page
        if len(page) < PAGE_SIZE:
            return bugs


def by_assignee(bugs: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for bug in bugs:
        grouped[bug["assigned_to"].lower()].append(bug)
    return grouped


def by_cc(bugs: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for bug in bugs:
        for email in bug["cc"]:
            grouped[email.lower()].append(bug)
    return grouped


def summary_packages(summary: str) -> set[str]:
    return set(PACKAGE.findall(summary))


class Bugs:
    """Bugs grouped by assignee and by CC."""

    def __init__(self, bugs: list[dict]):
        self.assigned = by_assignee(bugs)
        self.cced = by_cc(bugs)

    def get(self, maintainer: Maintainer) -> list[dict]:
        """The bugs assigned to `maintainer`, and those assigned to someone
        else that CC them and name one of their packages, as Bugzilla assigns
        a package's bugs to its first maintainer and CCs the others."""
        email = maintainer.email
        packages = set(maintainer.packages)
        cced = [bug for bug in self.cced.get(email, [])
                if bug["assigned_to"].lower() != email
                and packages & summary_packages(bug["summary"])]
        return sorted(self.assigned.get(email, []) + cced, key=lambda bug: bug["id"])

    def count_involving(self, email: str) -> int:
        """The number of bugs assigned to or CCing `email`."""
        return len({bug["id"] for bug in self.assigned.get(email, []) + self.cced.get(email, [])})


def assignee_note(maintainer: Maintainer, bug: dict) -> str:
    """Who `bug` is assigned to, if not `maintainer`."""
    assignee = bug["assigned_to"]
    return "" if assignee.lower() == maintainer.email else f"assigned to {assignee}"


def notes(*parts: str) -> str:
    text = ", ".join(filter(None, parts))
    return f" ({text})" if text else ""


def item(bug: dict, note: str = "") -> Item:
    return Item(f"Bug #{bug['id']}", f": {bug['summary']}{note}", f"{URL}/{bug['id']}")


def counted(title: str, count: int, what: str, **params: str | list[str]) -> Section:
    """Count the `what`s that `params` select on buglist.cgi, and link to them."""
    query = urlencode({**params, "product": PRODUCT}, doseq=True)
    return Section(title, omitted=f"{count} {what}{'' if count == 1 else 's'} not shown",
                   omitted_url=f"{URL}/buglist.cgi?{query}")


def section(title: str, bugs: list[dict],
            note: Callable[[dict], str] = lambda bug: "") -> Section:
    """List bugs, but only count and link to stabilization and keywording bugs."""
    items = [item(bug, note(bug)) for bug in bugs
             if bug["component"] not in ARCH_COMPONENTS]
    arch = [str(bug["id"]) for bug in bugs if bug["component"] in ARCH_COMPONENTS]
    return Section(title, items, omitted=omitted_text(len(arch), "bug"),
                   omitted_url=f"{URL}/buglist.cgi?bug_id={','.join(arch)}" if arch else "")


class BugsCollector(Collector):
    async def setup(self) -> None:
        self.bugs = Bugs(await search(self.config.bugzilla_api_key, status=OPEN))

    async def collect(self, maintainer: Maintainer) -> list[Section]:
        # Too many open bugs to list
        email = maintainer.email
        if email == MAINTAINER_NEEDED:
            return [counted("Open Bugs", len(self.bugs.assigned.get(email, [])), "open bug",
                            assigned_to=email, bug_status=OPEN)]
        if email == PROXY_MAINT:
            # CCed on the bugs of every proxied package
            return [counted("Open Bugs", self.bugs.count_involving(email), "open bug",
                            **PROXY_MAINT_QUERY, bug_status=OPEN)]
        return [section("Open Bugs", self.bugs.get(maintainer), lambda bug: notes(
            f"last changed {bug['last_change_time'][:10]}", assignee_note(maintainer, bug)))]


class NewBugsCollector(Collector):
    """Bugs filed and closed in the month reported on."""

    async def setup(self) -> None:
        # Bugzilla's time zone is UTC. Both ends of the range are included.
        first, last = self.month
        self.dates = {"chfieldfrom": first.isoformat(), "chfieldto": last.isoformat()}
        api_key = self.config.bugzilla_api_key
        new, closed = await asyncio.gather(
            search(api_key, chfield="[Bug creation]", **self.dates),
            search(api_key, status=CLOSED, chfield="bug_status",
                   chfieldvalue="RESOLVED", **self.dates),
        )
        self.new = Bugs(new)
        self.closed = Bugs(closed)

    async def collect(self, maintainer: Maintainer) -> list[Section]:
        filed = f"Bugs Filed in {self.month_name}"
        closed = f"Bugs Closed in {self.month_name}"
        if maintainer.email == PROXY_MAINT:
            # A hundred or more of each a month, too many to list
            return [
                counted(filed, self.new.count_involving(PROXY_MAINT), "bug",
                        **PROXY_MAINT_QUERY, chfield="[Bug creation]", **self.dates),
                counted(closed, self.closed.count_involving(PROXY_MAINT), "bug",
                        **PROXY_MAINT_QUERY, bug_status=CLOSED, chfield="bug_status",
                        chfieldvalue="RESOLVED", **self.dates),
            ]

        def note(bug: dict) -> str:
            return notes(assignee_note(maintainer, bug))

        return [
            section(filed, self.new.get(maintainer), note),
            section(closed, self.closed.get(maintainer), note),
        ]
