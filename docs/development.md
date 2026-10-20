# Working on rpgsync

## Set up

The tests run on EasyRPG's test games, from [the fork](https://github.com/oulianov/TestGame)
with the edits they use, cloned next to rpgsync (or set `TESTGAME=/path/to/TestGame`):

```bash
git clone https://github.com/oulianov/rpgsync.git
git clone https://github.com/oulianov/TestGame.git
cd rpgsync
uv run pytest
uv run ruff check && uv run ruff format --check
```

A few tests also read a local copy of a complete game or liblcf's CSVs, and are
skipped without them.

## Use your rpgsync in a game

Install your clone as the `rpgsync` command:

```bash
uv tool install --editable .
```

and link it into the game's scripts folder, as `.rpgsync/rpgsync` (kept out of git):

```bash
ln -s /path/to/rpgsync /path/to/MyGame/Scripts/.rpgsync/rpgsync
```

(Windows: `mklink /J C:\path\to\MyGame\Scripts\.rpgsync\rpgsync C:\path\to\rpgsync`)

Each rpgsync command then installs that folder, editable, in the scripts'
environment: your editor, the tests and `rpgsync check` use your rpgsync, while
`pyproject.toml` and `uv.lock` stay as they are for the others. `uv sync` and `uv run`
put the locked release back until the next rpgsync command: run the tests with
`uv run --no-sync pytest`. To go back to the release, delete the link and run `uv sync`.

uv can't override a locked package on one computer only (`[tool.uv.sources]` is only
read from `pyproject.toml`, and `uv.lock` records it), hence this link.

### Games set up before 0.1.0

They name the link in `Scripts/pyproject.toml` (`[tool.uv.sources]`), and rpgsync
still makes it for them. To move one to the PyPI release: replace its `rpgsync`
dependency with `rpgsync~=0.1.0`, delete the `[tool.uv.sources]` line of rpgsync, run
`uv lock` in `Scripts/`, and commit both files.

## Releasing

[CHANGELOG.md](../CHANGELOG.md) says when the minor or the patch version goes up.

1. Move the entries of *Unreleased* in CHANGELOG.md under the new version.
2. `uv version --bump minor` (or `patch`), commit, and push.
3. Publish a GitHub release whose tag is `v` and the version, on master:
   `gh release create v0.3.0 --generate-notes`, or Releases > Draft a new release on
   GitHub.
4. Run the [Release workflow](../.github/workflows/release.yml) from master, with
   "Attach the sdist and wheel to the latest release" and "Publish to PyPI": Actions >
   Release > Run workflow, or

   ```bash
   gh workflow run release.yml -f attach-to-latest-release=true -f publish-to-pypi=true
   ```

The workflow checks that the latest release's tag is pyproject.toml's version (it warns
when the tag is another commit than the one built), runs the tests on Python 3.11 to
3.15, builds, then attaches and publishes. Nothing is attached or published when a step
fails, and a run again replaces the attached files and skips those PyPI has already.
Without either option, it only tests and builds.

The first time only, PyPI must trust the workflow: on
<https://pypi.org/manage/account/publishing/>, "Add a new pending publisher" (GitHub)
with PyPI project name `rpgsync`, owner `oulianov`, repository `rpgsync`, workflow name
`release.yml`, and no environment name. No token is needed.
