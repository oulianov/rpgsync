# Changelog

## Versions

rpgsync is released on [PyPI](https://pypi.org/project/rpgsync/) as `0.MINOR.PATCH`:

- **MINOR** goes up when the scripts change: `rpgsync pull` writes a game's scripts
  differently (another layout, other names, another way to write a command), or scripts
  written by it may not work with the previous minor version (a DSL name renamed or
  removed).
- **PATCH** goes up for everything else: fixes and additions after which `rpgsync pull`
  writes the same scripts.

A game's `Scripts/pyproject.toml` depends on the version that set it up,
`rpgsync~=0.MINOR.PATCH` (that version and its later patches), and its `Scripts/uv.lock`
pins the exact one. [Upgrading rpgsync](README.md#upgrading-rpgsync) moves a game to a
newer one; [Releasing](docs/development.md#releasing) makes a new one.

## Unreleased

## 0.1.0

First release on PyPI.

- A new game's scripts depend on `rpgsync~=0.1.0` from PyPI, pinned by their `uv.lock`,
  instead of a link to this computer's rpgsync folder.
- A developer of rpgsync links their rpgsync folder as `Scripts/.rpgsync/rpgsync`:
  rpgsync installs it, editable, in the scripts' environment, and leaves
  `pyproject.toml` and `uv.lock` as they are.
