# SPDX-License-Identifier: GPL-3.0-or-later

import re
from collections import defaultdict

from .base import PackageTableCollector, package_link
from .. import ci

AGE = re.compile(r"no change in (\d+) days")
COLUMNS = ["Package", "Version", "Days unchanged"]


def age(msg: str) -> str:
    """The days in a StableRequest message such as "slot(0) no change in
    120 days for unstable keywords: [ ~amd64, ~x86 ]"."""
    match = AGE.search(msg)
    return match[1] if match else ""


class StableRequestsCollector(PackageTableCollector):
    """pkgcheck's StableRequest results in the latest CI report."""
    title = "Packages Needing Stable Requests"
    columns = COLUMNS

    async def setup(self) -> None:
        results = await ci.report(self.config.cache_dir)
        self.rows = defaultdict(list)
        for r in results:
            if r["class"] == "StableRequest":
                package = ci.package_of(r)
                self.rows[package].append([package_link(package), r["version"], age(r["msg"])])
