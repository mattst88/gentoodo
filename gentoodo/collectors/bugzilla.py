# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import json
from collections import defaultdict
from collections.abc import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode

from .base import Collector, Item, Section, omitted_text
from ..maintainers import MAINTAINER_NEEDED, Maintainer
from ..util import fetch

URL = "https://bugs.gentoo.org"
PAGE_SIZE = 10000  # the most the server returns per request
FIELDS = "id,summary,component,assigned_to,last_change_time"
ARCH_COMPONENTS = {"Stabilization", "Keywording"}
PRODUCT = "Gentoo Linux"  # package bugs
OPEN = ["UNCONFIRMED", "CONFIRMED", "IN_PROGRESS"]


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


def item(bug: dict, note: str = "") -> Item:
    return Item(f"Bug #{bug['id']}", f": {bug['summary']}{note}", f"{URL}/{bug['id']}")


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
        self.bugs = by_assignee(await search(self.config.bugzilla_api_key, status=OPEN))

    async def collect(self, maintainer: Maintainer) -> list[Section]:
        bugs = self.bugs.get(maintainer.email, [])
        if maintainer.email == MAINTAINER_NEEDED:
            # Thousands, too many to list
            query = urlencode({"assigned_to": MAINTAINER_NEEDED, "bug_status": OPEN,
                               "product": PRODUCT}, doseq=True)
            return [Section("Open Bugs", omitted=f"{len(bugs)} open bugs not shown",
                            omitted_url=f"{URL}/buglist.cgi?{query}")]
        return [section("Open Bugs", bugs,
                        lambda bug: f" (last changed {bug['last_change_time'][:10]})")]


class NewBugsCollector(Collector):
    """Bugs filed and closed in the month reported on."""

    async def setup(self) -> None:
        # Bugzilla's time zone is UTC. Both ends of the range are included.
        first, last = self.month
        dates = {"chfieldfrom": first.isoformat(), "chfieldto": last.isoformat()}
        api_key = self.config.bugzilla_api_key
        new, closed = await asyncio.gather(
            search(api_key, chfield="[Bug creation]", **dates),
            search(api_key, status=["RESOLVED", "VERIFIED"], chfield="bug_status",
                   chfieldvalue="RESOLVED", **dates),
        )
        self.new = by_assignee(new)
        self.closed = by_assignee(closed)

    async def collect(self, maintainer: Maintainer) -> list[Section]:
        return [
            section(f"Bugs Filed in {self.month_name}",
                    self.new.get(maintainer.email, [])),
            section(f"Bugs Closed in {self.month_name}",
                    self.closed.get(maintainer.email, [])),
        ]
