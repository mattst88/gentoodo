# SPDX-License-Identifier: GPL-3.0-or-later

"""What the collectors found for each maintainer, saved so that the emails
can be made again without querying anything, to work on how they are
rendered."""

import json
from dataclasses import asdict
from pathlib import Path

from .collectors.base import Item, Section
from .maintainers import Maintainer

FORMAT = 1

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


def load(path: Path) -> Results:
    """Read what `save()` wrote, raising ValueError if it cannot be."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != FORMAT:
            raise ValueError(f"format {data.get('format')}, not {FORMAT}")
        return [
            (Maintainer(**entry["maintainer"]),
             [(section.pop("collector"),
               Section(**section | {"items": [Item(**i) for i in section["items"]]}))
              for section in entry["sections"]])
            for entry in data["maintainers"]
        ]
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as e:
        raise ValueError(f"not saved by --save-data: {e!r}") from e
