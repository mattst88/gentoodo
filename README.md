# gentoodo

Writes a monthly reminder email for each Gentoo package maintainer,
listing:

- for a proxy, such as proxy-maint@gentoo.org, when each maintainer they
  are the proxy for last authored a commit, oldest first: those listed
  with `proxied="yes"` on the packages where the proxy is listed with
  `proxied="proxy"` or `proxied="maintainer-and-proxy"`, as GLEP 67 has
  it
- their packages with a newer upstream version, from packages.gentoo.org
  (which gets them from Repology)
- their package versions that are ready for stabilization, from Gentoo's
  CI
- the open GitHub and Codeberg pull requests that touch their packages
- the commits to their packages in the previous month (for a project,
  split into commits by its members and by others, with a count of the
  others' commits that each member committed)
- the pkgcheck errors and warnings new in the previous month, from
  Gentoo's CI
- their redundant package versions, from Gentoo's CI
- the bugs assigned to them that were filed or closed in the previous
  month, and those still open, from the Gentoo Linux product of
  bugs.gentoo.org

Stabilization and keywording commits and bugs are counted rather than
listed.

Packages without a maintainer are reported to
maintainer-needed@gentoo.org, the assignee of their bugs. Its open bugs,
of which there are thousands, are counted and linked rather than listed.

Maintainers with nothing to report get no email. The emails are written
as `.eml` files, and sent in a separate step. If GitHub or Codeberg
cannot be read, the emails say so in place of its pull requests.

## Requirements

- Python 3.11 or later
- the [GitHub CLI](https://cli.github.com/), logged in, or a token in the
  file named by `secret_file` in the `[github]` section of the configuration
- a [bugs.gentoo.org API key](https://bugs.gentoo.org/userprefs.cgi?tab=apikey),
  in the file named by `secret_file` in the `[bugzilla]` section of the
  configuration

gentoo.git is cloned into `cache_dir` on the first run and fetched on
each run after. Only the `metadata.xml` files, `.mailmap` and the history
are read, so the clone is a bare partial clone. To use a repository of
your own instead, set `gentoo_repo`. It can be bare, but must have its
history: a shallow clone gives wrong reports. Its `metadata.xml` files
are read as committed in `HEAD`.

## Usage

    pip install .
    cp config.example.toml config.toml
    gentoodo --config config.toml --dry-run --maintainer larry@gentoo.org

`--dry-run` prints the email instead of writing it to `output_dir`.
Without `--maintainer`, every maintainer in the repository is processed.
New QA warnings, commits, and bugs filed and closed are those of the
previous month, the one before the current month in UTC, so run it early
on the first of each month. `--month 2026-10` reports on October 2026
instead, up to now if it is the current month.

Unless `--no-pull` is given, the repository is first updated with
`git fetch`, or with `git pull --ff-only` if it is not bare.
`--collectors` limits the run to some of
`proxied`, `outdated`, `stable_requests`, `github_prs`, `codeberg_prs`,
`shortlog`, `qa`, `redundant`, `new_bugs` and `bugs`.

The pkgcheck reports of Gentoo's CI come from
[qa-reports.gentoo.org](https://qa-reports.gentoo.org/), found by date
in the commits of
[gentoo-ci.git](https://anongit.gentoo.org/git/report/gentoo-ci.git),
kept in `cache_dir`. New QA warnings are those in the last report of the
previous month and not in the last report before it, ignoring versions,
line numbers and bracketed lists, and checks with no results in the
earlier report.

To look over what was written in a browser before sending it:

    gentoodo-preview ~/.local/share/gentoodo/outbox
    firefox ~/.local/share/gentoodo/outbox/preview/index.html

Each email is written as an HTML page in `preview` in the directory
given, or in the directory given with `--output`, with an `index.html`
linking to them all. An email with no HTML part, written with
`multipart = false`, is shown as its plain text.

To work on how the emails are rendered without querying everything again
each time, save what was found for each maintainer, and make the emails
again from it:

    gentoodo --config config.toml --save-data november.json
    gentoodo --config config.toml --from-data november.json

`--from-data` queries nothing, not even the repository, and reports on
the month the data was saved for. The sections are put in the order the
collectors run in now, and `--collectors` and `--maintainer` choose
among them. Changes to the templates and the configuration show up, but
not changes to the collectors, which make the text of each item.

To send what was written, with the `sendmail` of the configuration:

    gentoodo --config config.toml --send-outbox

Each email sent is moved to `sent` in `output_dir`. Those that fail stay
behind, to be sent by the next `--send-outbox`. With `--dry-run`, it
lists what it would send. With `--maintainer`, it sends only the emails
to that maintainer, leaving the rest in `output_dir`.

To copy every email to a mailing list, set `cc` in the `[general]`
section of the configuration. The list receives the same emails as
maintainers, including any restricted bugs the Bugzilla API key can see.

## Proxied maintainers

`gentoodo-proxied` is a separate command, run by hand when a proxy, such
as proxy-maint, wants to look into its proxied maintainers. It prints
its report and sends no email: a proxy's monthly email has only the
table of last commits. It reports on each proxied maintainer, each one
listed with `proxied="yes"` in gentoo.git:

- when they last authored a commit, following `.mailmap`
- the commit that added them to each of their packages, and why, following
  changes of address under the same name back to the one they were added
  under
- other addresses with their name or local part that committed to their
  packages more recently than they did, which may need `.mailmap` entries
- their GURU packages and last commit there, with `guru_repo` set
- their proxied maintainer tracker bug, the other open bugs assigned to
  them, and when they last commented on Bugzilla, with a Bugzilla API key

    gentoodo-proxied --config config.toml [--proxy EMAIL] [--never] [--json] [EMAIL ...]

Without addresses, it reports on all of them. `--proxy` limits it to
those a developer or project is listed as the proxy for, with
`proxied="proxy"` or `proxied="maintainer-and-proxy"`, and to the
packages it is their proxy on. `--never` limits it to those who never
authored a commit. Those listed in gentoo.git's first commit were added
in CVS, found with `historical_repo` set to a clone of historical.git. To
make reading each package's history fast, it writes a commit-graph with
changed-path Bloom filters into gentoo.git and historical.git, which
takes a minute or two the first time.

## Limitations

- Pull requests are matched on the first 100 files they change.

## Tests

    pip install '.[test]'
    pytest

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
