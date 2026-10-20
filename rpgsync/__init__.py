"""rpgsync - edit RPG Maker 2000/2003 events as text, synced live with the game files."""

from importlib import metadata

try:
    __version__ = metadata.version("rpgsync")  # pyproject.toml's, as installed
except metadata.PackageNotFoundError:  # a source folder that was never installed
    __version__ = "0+unknown"
