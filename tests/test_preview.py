# SPDX-License-Identifier: GPL-3.0-or-later

from dataclasses import replace
from pathlib import Path

import pytest

from gentoodo.collectors.base import Item, Section
from gentoodo.config import Config
from gentoodo.email import render
from gentoodo.maintainers import Maintainer
from gentoodo.preview import main, preview

CONFIG = Config(gentoo_repo=Path("/nonexistent"))
SECTIONS = [Section("Open Bugs", [Item("Bug #1", ": <b> & ünïcode",
                                       "https://bugs.gentoo.org/1")])]


def write(tmp_path, email, multipart=True):
    msg = render(replace(CONFIG, multipart=multipart),
                 Maintainer(email, packages=["x11-libs/libdrm"]), SECTIONS)
    path = tmp_path / f"{email.replace('@', '_at_')}.eml"
    path.write_bytes(msg.as_bytes())
    return path


def test_html(tmp_path):
    write(tmp_path, "x11@gentoo.org")

    index = preview(tmp_path, tmp_path / "preview")

    page = (tmp_path / "preview" / "x11_at_gentoo.org.html").read_text()
    assert page.startswith("<!DOCTYPE html>")
    assert '<a href="https://bugs.gentoo.org/1">Bug #1</a>: &lt;b&gt; &amp; ünïcode' in page
    assert index == tmp_path / "preview" / "index.html"
    assert ('<a href="x11_at_gentoo.org.html">x11@gentoo.org</a>'
            in index.read_text())
    assert "1 email from" in index.read_text()


def test_plain_text(tmp_path):
    write(tmp_path, "x11@gentoo.org", multipart=False)

    preview(tmp_path, tmp_path / "preview")

    page = (tmp_path / "preview" / "x11_at_gentoo.org.html").read_text()
    assert "<pre>Hi x11@gentoo.org," in page
    assert "Bug #1: &lt;b&gt; &amp; ünïcode" in page


def test_index_sorted(tmp_path):
    for email in ("zz@gentoo.org", "Aa@gentoo.org", "mm@gentoo.org"):
        write(tmp_path, email)

    index = preview(tmp_path, tmp_path / "out").read_text()

    assert (index.index("Aa@gentoo.org") < index.index("mm@gentoo.org")
            < index.index("zz@gentoo.org"))
    assert "3 emails from" in index


def test_main_default_output(tmp_path, capsys):
    write(tmp_path, "x11@gentoo.org")

    main([str(tmp_path)])

    assert capsys.readouterr().out == f"{tmp_path / 'preview' / 'index.html'}\n"
    # Left where --send-outbox does not look
    assert [p.name for p in tmp_path.glob("*.html")] == []


def test_main_not_a_directory(tmp_path):
    with pytest.raises(SystemExit, match="Not a directory"):
        main([str(tmp_path / "nonexistent")])
