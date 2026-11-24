# SPDX-License-Identifier: GPL-3.0-or-later

"""What the collectors found for each maintainer, saved so that the emails
can be made again without querying anything, to work on how they are
rendered."""

import json
from dataclasses import asdict
from pathlib import Path

from .collectors.base import Cell, Item, Link, Section
from .maintainers import Commit, Maintainer

FORMAT = 2

# Each maintainer, with the sections found for them, each with the name of
# the collector that found it
Results = list[tuple[Maintainer, list[tuple[str, Section]]]]


def save(path: Path, results: Results) -> None:
    data = {
        "format": FORMAT,
        "maintainers": [
            {"maintainer": asdict(maintainer),
             "sections": [{"collector": name, **asdict(section)}
                          for name, section in sections]}
            for maintainer, sections in results
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def cell(value: str | dict | list) -> Cell:
    """A table cell as `save()` wrote it."""
    if isinstance(value, dict):
        return Link(**value)
    if isinstance(value, list):
        return [Link(**link) for link in value]
    return value


def maintainer(value: dict) -> Maintainer:
    """A maintainer as `save()` wrote it."""
    commits = {key: Commit(**value[key])
               for key in ("last_authored", "last_committed") if value.get(key)}
    return Maintainer(**value | commits)


def load(path: Path) -> Results:
    """Read what `save()` wrote, raising ValueError if it cannot be."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != FORMAT:
            raise ValueError(f"format {data.get('format')}, not {FORMAT}")
        return [
            (maintainer(entry["maintainer"]),
             [(section.pop("collector"),
               Section(**section | {
                   "items": [Item(**i) for i in section["items"]],
                   "rows": [[cell(c) for c in row] for row in section.get("rows", [])],
               }))
              for section in entry["sections"]])
            for entry in data["maintainers"]
        ]
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as e:
        raise ValueError(f"not saved by --save-data: {e!r}") from e
