# SPDX-License-Identifier: GPL-3.0-or-later

from datetime import datetime

from .base import Collector, Section
from ..maintainers import Commit, Maintainer

COLUMNS = ["Last authored", "Maintainer", "Commit"]


def by_last_authored(last_authored: Commit | None) -> tuple[bool, datetime]:
    """A sort key: those who never authored a commit first, then oldest
    first, comparing times across time zones."""
    if not last_authored:
        return False, datetime.min
    return True, datetime.fromisoformat(last_authored.date)


def table(maintainers: list[Maintainer]) -> list[list[str]]:
    """Each maintainer's last authored commit, sorted by by_last_authored()."""
    rows = []
    for m in sorted(maintainers, key=lambda m: (by_last_authored(m.last_authored), m.email)):
        if c := m.last_authored:
            rows.append([c.day, m.email, c.subject])
        else:
            rows.append(["never", m.email, ""])
    return rows


class ProxiedMaintainersCollector(Collector):
    """For a proxy, such as proxy-maint, when each maintainer they are the
    proxy for last authored a commit.

    Those are the maintainers listed with proxied="yes" on the packages
    where the proxy is listed with proxied="proxy" or
    proxied="maintainer-and-proxy", as GLEP 67 has it.
    """

    # All maintainers, set by the caller, to look up each proxied
    # maintainer's last commit (see with_proxied())
    maintainers: dict[str, Maintainer]

    async def collect(self, maintainer: Maintainer) -> list[Section]:
        if not maintainer.proxied_maintainers:
            return []
        return [Section("Proxied Maintainers by Last Authored Commit", columns=COLUMNS,
                        rows=table([self.maintainers[e]
                                    for e in maintainer.proxied_maintainers]))]


def with_proxied(selected: list[Maintainer],
                 maintainers: dict[str, Maintainer]) -> list[Maintainer]:
    """`selected`, and the maintainers any of them is the proxy for."""
    people = {m.email: m for m in selected}
    for m in selected:
        for email in m.proxied_maintainers:
            people.setdefault(email, maintainers[email])
    return list(people.values())
