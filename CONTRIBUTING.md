# Contributing

GitHub is the primary source for code, public documentation, issues and pull
requests. Work on a focused branch and open a PR targeting main. Explain the
problem, resulting behavior and relevant validation. Update README.md and
README.de.md together when changing their shared content.

Run the tests using one of the pinned environments described in
[testing](docs/testing.md). Public tests use mocks; do not add installation
credentials, private host addresses, raw household data or deployment access.

Link fully resolved issues with closing keywords in the PR body. For partial
work, link the issue normally and describe what remains. Contributions that are
reimplemented or superseded should be acknowledged and linked honestly; closing a
PR does not mean its commits were merged.

PR checks run without production access. A code merge is not a production
deployment. Releases are explicit, with candidates marked as prereleases. Changes
to optimizer/planner behavior need regression tests for budgeting and safety
precedence; doc-only changes do not need tests that simply repeat the prose.

Installation-specific operational notes and local configuration are maintained separately and
are not required to build or test the public project.
