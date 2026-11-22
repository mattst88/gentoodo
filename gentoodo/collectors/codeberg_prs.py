# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import json
import logging
import os
import re

from ..util import fetch_with_headers, run
from .pull_requests import PullRequest, PullRequestsCollector

log = logging.getLogger(__name__)

URL = "https://codeberg.org"

# Forgejo's largest page size by default
PAGE_SIZE = 50

# Pull requests whose files are listed at once
CONCURRENCY = 8

MISSING_REF = re.compile(r"couldn't find remote ref (\S+)")


def next_link(header: str | None) -> str | None:
    """Return the URL with rel="next" in a Link header, if any."""
    for link in (header or "").split(","):
        url, *params = (part.strip() for part in link.split(";"))
        if any(param.replace('"', "") == "rel=next" for param in params):
            return url.strip("<>")
    return None


async def fetch_pages(url: str) -> list:
    """GET each page of a paginated Forgejo API list."""
    results = []
    while url:
        data, headers = await asyncio.to_thread(fetch_with_headers, url)
        results += json.loads(data)
        url = next_link(headers.get("Link"))
    return results


class CodebergPRsCollector(PullRequestsCollector):
    title = "Open Codeberg Pull Requests"
    forge = "Codeberg"

    async def fetch(self) -> list[PullRequest]:
        # https://codeberg.org/api/swagger#/repository/repoListPullRequests
        # A pull request that could not be retrieved is null in the list.
        prs = [pr for pr in await fetch_pages(
            f"{URL}/api/v1/repos/{self.config.codeberg_repo}/pulls"
            f"?state=open&limit={PAGE_SIZE}") if pr]
        if not prs:
            return []

        # The files of each pull request are read from git, not from the
        # API's list of them. Codeberg counts each of those lists as a git
        # operation, of which it allows 250 every 10 minutes, too few for a
        # request for each pull request, but one fetch of them all counts as
        # one operation.
        repo = str(self.config.gentoo_repo)
        # Fetch refs/pull/N/head, which covers forks too (the API gives only
        # the branch name). No refs or FETCH_HEAD are written, since another
        # fetch may be writing them; the objects are found by the SHAs in `prs`.
        heads = {f"refs/pull/{pr['number']}/head": pr for pr in prs}
        while heads:
            try:
                await run("git", "-C", repo, "fetch", "--quiet", "--no-write-fetch-head",
                          "--no-tags", f"{URL}/{self.config.codeberg_repo}.git", *heads,
                          timeout=900,
                          # In English, to find the head it names
                          env=os.environ | {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
                break
            except RuntimeError as e:
                # Closed, with its head deleted, since it was listed. git
                # names only the first such head, so fetch again without it.
                if not (m := MISSING_REF.search(str(e))) or m[1] not in heads:
                    raise
                log.warning("Skipping Codeberg PR #%d: closed since it was listed",
                            heads.pop(m[1])["number"])
        prs = list(heads.values())

        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def files(pr: dict) -> PullRequest | None:
            # No rename detection, which would fetch the contents of files
            # a partial clone lacks, so a renamed file is listed under its
            # old and new names.
            async with semaphore:
                try:
                    out = await run("git", "-C", repo, "diff", "--name-status", "-z",
                                    "--no-renames", pr["merge_base"], pr["head"]["sha"])
                except RuntimeError as e:
                    # e.g. pushed to after it was listed
                    log.warning("Skipping Codeberg PR #%d: %s", pr["number"], e)
                    return None
            # Each file's status, then its name
            fields = out.split("\0")[:-1]
            changes = list(zip(fields[::2], fields[1::2]))
            return PullRequest(pr["number"], pr["title"], pr["html_url"],
                               [name for _, name in changes],
                               [name for status, name in changes if status == "A"])

        return [pr for pr in await asyncio.gather(*map(files, prs)) if pr]
