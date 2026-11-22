# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from . import __version__, data, repo
from .collectors.base import Collector, Section
from .collectors.bugzilla import BugsCollector, NewBugsCollector
from .collectors.codeberg_prs import CodebergPRsCollector
from .collectors.github_prs import GitHubPRsCollector
from .collectors.outdated import OutdatedCollector
from .collectors.qa import QACollector
from .collectors.redundant import RedundantVersionsCollector
from .collectors.shortlog import ShortlogCollector
from .collectors.stable_requests import StableRequestsCollector
from .config import Config, load_config
from .email import render, send_outbox, write_email
from .maintainers import Maintainer, find_last_commits, load_maintainers
from .util import month_days

log = logging.getLogger(__name__)

# In the order their sections appear in the email
COLLECTORS = {
    "outdated": OutdatedCollector,
    "stable_requests": StableRequestsCollector,
    "github_prs": GitHubPRsCollector,
    "codeberg_prs": CodebergPRsCollector,
    "shortlog": ShortlogCollector,
    "qa": QACollector,
    "redundant": RedundantVersionsCollector,
    "new_bugs": NewBugsCollector,
    "bugs": BugsCollector,
}


def collector_names(value: str) -> list[str]:
    names = value.split(",")
    if unknown := set(names) - set(COLLECTORS):
        raise argparse.ArgumentTypeError(
            f"unknown collector {', '.join(sorted(unknown))} "
            f"(available: {', '.join(COLLECTORS)})")
    return [name for name in COLLECTORS if name in names]


def positive_int(value: str) -> int:
    if (n := int(value)) < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {value}")
    return n


def month(value: str) -> tuple[date, date]:
    try:
        return month_days(datetime.strptime(value, "%Y-%m").date())
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a month like 2026-10: {value}") from None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gentoodo",
        description="Generate reminder emails for Gentoo package maintainers",
    )
    parser.add_argument("--config", required=True,
                        help="path to the TOML configuration file")
    parser.add_argument("--maintainer",
                        help="only process the maintainer with this email address, "
                             "or with --send-outbox, only send to them")
    parser.add_argument("--dry-run", action="store_true",
                        help="print emails instead of writing them, or with "
                             "--send-outbox, list those it would send")
    parser.add_argument("--send-outbox", action="store_true",
                        help="send the emails in output_dir instead of writing new ones")
    parser.add_argument("--collectors", type=collector_names, default=list(COLLECTORS),
                        help="comma-separated collectors to run (default: all)")
    parser.add_argument("--month", type=month,
                        help="report on this month, as YYYY-MM (default: the previous one)")
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--save-data", type=Path, metavar="FILE",
                         help="also save what was found for each maintainer, as JSON")
    sources.add_argument("--from-data", type=Path, metavar="FILE",
                         help="make the emails from what --save-data saved, "
                              "querying nothing")
    parser.add_argument("--no-pull", action="store_true",
                        help="do not update the gentoo repository first")
    parser.add_argument("-j", "--jobs", type=positive_int, default=8,
                        help="maintainers to process concurrently (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="enable debug logging")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.from_data and args.month:
        parser.error("--month cannot be used with --from-data, which reports on "
                     "the month it was saved for")
    if args.send_outbox and (args.save_data or args.from_data):
        parser.error("--send-outbox cannot be used with --save-data or --from-data")
    return args


async def collect(collector: Collector, maintainer: Maintainer) -> list[Section]:
    try:
        return await collector.collect(maintainer)
    except Exception as e:
        log.warning("%s failed for %s: %s", type(collector).__name__,
                    maintainer.email, e, exc_info=log.isEnabledFor(logging.DEBUG))
        return []


def write(config: Config, maintainer: Maintainer, sections: list[Section],
          dry_run: bool) -> None:
    """Write the email of `sections` to `maintainer`, if there is anything in
    them worth one."""
    sections = [s for s in sections if s.content or s.omitted]
    # Counts of what was left out are not worth an email on their own.
    if all(s.notice or not s.content for s in sections):
        log.info("Nothing to report for %s", maintainer.email)
        return

    try:
        msg = render(config, maintainer, sections)
        if dry_run:
            print(msg.as_bytes().decode())
            return
        log.info("Wrote %s", write_email(config.output_dir, maintainer.email, msg))
    except Exception:
        log.exception("Failed to write reminder for %s", maintainer.email)


async def process(config: Config, maintainer: Maintainer,
                  collectors: dict[str, Collector], dry_run: bool,
                  semaphore: asyncio.Semaphore) -> list[tuple[str, Section]]:
    """Write the email to `maintainer`, and return its sections, each with
    the name of the collector that found it."""
    async with semaphore:
        results = await asyncio.gather(
            *(collect(c, maintainer) for c in collectors.values()))
        sections = [(name, s) for name, result in zip(collectors, results)
                    for s in result]
        write(config, maintainer, [s for _, s in sections], dry_run)
        return sections


def from_data(config: Config, args: argparse.Namespace) -> None:
    try:
        results = data.load(args.from_data)
    except (OSError, ValueError) as e:
        sys.exit(f"Failed to read {args.from_data}: {e}")
    if args.maintainer:
        results = [r for r in results if r[0].email == args.maintainer.lower()]
        if not results:
            sys.exit(f"{args.maintainer} is not in {args.from_data}")
    # In the order of COLLECTORS now, not when saved, and without collectors
    # no longer run or not chosen with --collectors
    order = {name: i for i, name in enumerate(args.collectors)}
    for maintainer, sections in results:
        sections = sorted((p for p in sections if p[0] in order), key=lambda p: order[p[0]])
        write(config, maintainer, [s for _, s in sections], args.dry_run)


async def async_main(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.from_data:
        from_data(config, args)
        return

    try:
        config.gentoo_repo = await repo.update(config, not args.no_pull)
    except repo.CloneError as e:
        sys.exit(f"Failed to clone {repo.URL}: {e}")
    except RuntimeError as e:
        sys.exit(f"{e}\nUse --no-pull to run on the repository as it is.")

    try:
        maintainers = load_maintainers(config.gentoo_repo)
    except RuntimeError as e:
        sys.exit(f"Failed to read maintainers: {e}")
    log.info("Found %d maintainers", len(maintainers))
    if args.maintainer:
        maintainer = maintainers.get(args.maintainer.lower())
        if maintainer is None:
            sys.exit(f"{args.maintainer} maintains no packages in {config.gentoo_repo}")
        selected = [maintainer]
    else:
        selected = list(maintainers.values())

    collectors = {name: COLLECTORS[name](config, args.month) for name in args.collectors}
    try:
        await asyncio.gather(
            asyncio.to_thread(find_last_commits, config.gentoo_repo, selected),
            *(c.setup() for c in collectors.values()),
        )
    except Exception as e:
        sys.exit(f"Failed to fetch data: {e}")

    semaphore = asyncio.Semaphore(args.jobs)
    results = await asyncio.gather(*(
        process(config, m, collectors, args.dry_run, semaphore)
        for m in selected
    ))

    for collector in collectors.values():
        collector.finish()

    if args.save_data:
        try:
            data.save(args.save_data, list(zip(selected, results)))
        except OSError as e:
            sys.exit(f"Failed to save {args.save_data}: {e}")
        log.info("Saved %s", args.save_data)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
        # Importing pkgcheck adds a handler, which would make this do nothing.
        force=True,
    )
    if args.send_outbox:
        config = load_config(args.config)
        try:
            failed = send_outbox(config.output_dir, config.sendmail, args.dry_run,
                                 args.maintainer)
        except OSError as e:
            sys.exit(f"Failed to send: {e}")
        if failed:
            sys.exit(f"{failed} emails not sent, left in {config.output_dir}")
        return
    asyncio.run(async_main(args))
