# SPDX-License-Identifier: GPL-3.0-or-later

"""Write each generated .eml file as an HTML page, with an index linking to
them all, to look over the emails in a browser before they are sent."""

import argparse
import email.policy
import logging
import sys
from email import message_from_bytes
from html import escape
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

INDEX = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>gentoodo preview</title>
<style>
body{{font-family:sans-serif;max-width:900px;margin:0 auto;color:#222;line-height:1.4}}
table{{border-collapse:collapse;width:100%}}
th,td{{text-align:left;padding:3px 8px;border-bottom:1px solid #eee}}
a{{color:#0060a0}}
.meta{{color:#555;font-size:.9em}}
</style>
</head>
<body>
<h1>gentoodo preview</h1>
<p class="meta">{count} email{plural} from {source}</p>
<table>
<tr><th>To</th><th>Subject</th><th>Date</th></tr>
{rows}
</table>
</body>
</html>
"""

TEXT_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{subject}</title>
</head>
<body>
<pre>{text}</pre>
</body>
</html>
"""


def page(msg) -> str:
    """The HTML part of `msg`, or its plain text in a <pre> if it has none."""
    body = msg.get_body(preferencelist=("html", "plain"))
    if body is None:
        raise ValueError("no text or HTML part")
    if body.get_content_type() == "text/html":
        return body.get_content()
    return TEXT_PAGE.format(subject=escape(msg["Subject"] or ""),
                            text=escape(body.get_content()))


def preview(source: Path, output: Path) -> Path:
    """Write each .eml file in `source` to `output` as an HTML page of the
    same name, and an index.html linking to them. Return the index's path."""
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted(source.glob("*.eml")):
        msg = message_from_bytes(path.read_bytes(), policy=email.policy.default)
        try:
            html = page(msg)
        except ValueError as e:
            log.warning("Skipping %s: %s", path.name, e)
            continue
        name = f"{path.stem}.html"
        (output / name).write_text(html, encoding="utf-8")
        rows.append((msg["To"] or "", msg["Subject"] or "", msg["Date"] or "", name))

    index = output / "index.html"
    index.write_text(INDEX.format(
        count=len(rows),
        plural="" if len(rows) == 1 else "s",
        source=escape(str(source)),
        rows="\n".join(
            f'<tr><td><a href="{escape(name)}">{escape(to)}</a></td>'
            f"<td>{escape(subject)}</td><td>{escape(date)}</td></tr>"
            for to, subject, date, name in sorted(rows, key=lambda r: r[0].lower())),
    ), encoding="utf-8")
    return index


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gentoodo-preview",
        description="Write generated .eml files as HTML pages, with an index "
                    "linking to them all",
    )
    parser.add_argument("source", type=Path, metavar="DIR",
                        help="directory of .eml files, such as output_dir")
    parser.add_argument("-o", "--output", type=Path,
                        help="directory to write the pages to (default: DIR/preview)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s",
                        force=True)
    source = args.source.expanduser()
    if not source.is_dir():
        sys.exit(f"Not a directory: {source}")
    output = (args.output or source / "preview").expanduser()
    print(preview(source, output))
