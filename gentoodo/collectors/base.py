# SPDX-License-Identifier: GPL-3.0-or-later

from dataclasses import dataclass, field
from datetime import date

from pkgcore.ebuild.cpv import VersionedCPV

from ..config import Config
from ..maintainers import Maintainer
from ..util import previous_month


@dataclass(frozen=True)
class Item:
    """One line of a section, rendered as `ref` followed by `text`."""
    ref: str
    text: str = ""
    url: str | None = None


@dataclass(frozen=True)
class Link:
    """A table cell linking `text` to `url` in HTML."""
    text: str
    url: str


# A table cell: text, a link, or links shown one after another
Cell = str | Link | list[Link]


def cell_text(cell: Cell) -> str:
    if isinstance(cell, str):
        return cell
    if isinstance(cell, Link):
        return cell.text
    return ", ".join(link.text for link in cell)


def package_link(package: str) -> Link:
    return Link(package, f"https://packages.gentoo.org/packages/{package}")


def package_version(package: str, version: str) -> VersionedCPV:
    """`version` of `package`, which compares as versions do."""
    return VersionedCPV(f"{package}-{version}")


@dataclass
class Section:
    title: str
    items: list[Item] = field(default_factory=list)
    text: str = ""  # preformatted, shown instead of items
    # A table, shown instead of items, with a header of `columns` in HTML
    columns: list[str] = field(default_factory=list)
    rows: list[list[Cell]] = field(default_factory=list)
    urls_in_text: bool = False  # also print each item's URL in the text part
    notice: bool = False  # not worth an email on its own
    omitted: str = ""  # what was left out, shown after the items or text
    omitted_url: str = ""  # where to see what was left out

    @property
    def content(self) -> bool:
        return bool(self.items or self.text or self.rows)


def omitted_text(count: int, what: str) -> str:
    """Describe `count` stabilization and keywording `what`s left out."""
    if not count:
        return ""
    return f"{count} stabilization and keywording {what}{'s' if count != 1 else ''} not shown"


class Collector:
    def __init__(self, config: Config, month: tuple[date, date] | None = None):
        self.config = config
        # First and last days of the month reported on, by default the
        # previous one
        self.month = month or previous_month()
        self.month_name = f"{self.month[0]:%B %Y}"

    async def setup(self) -> None:
        """Fetch data shared by all maintainers."""

    async def collect(self, maintainer: Maintainer) -> list[Section]:
        raise NotImplementedError

    def finish(self) -> None:
        """Report on the run, once every maintainer has been processed."""


class PackageCollector(Collector):
    """A collector of items by package, set in `setup()`."""
    title: str
    urls_in_text = False
    items: dict[str, list[Item]]

    async def collect(self, maintainer: Maintainer) -> list[Section]:
        # An item can be under several of the maintainer's packages.
        items = dict.fromkeys(
            item
            for package in maintainer.packages
            for item in self.items.get(package, ())
        )
        return [Section(self.title, list(items), urls_in_text=self.urls_in_text)]


class PackageTableCollector(Collector):
    """A collector of table rows by package, set in `setup()`."""
    title: str
    columns: list[str]
    rows: dict[str, list[list[Cell]]]

    async def collect(self, maintainer: Maintainer) -> list[Section]:
        rows = [row for package in sorted(set(maintainer.packages))
                for row in self.rows.get(package, ())]
        return [Section(self.title, columns=self.columns, rows=rows)]
