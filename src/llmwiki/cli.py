"""The `wiki` console entry point.

Placeholder for the PoC scaffold. Later tasks wire the subcommands
(ingest / query / file-back / get / list / rebuild) onto this entry point.
"""

from __future__ import annotations

from . import __version__
from . import config


def main() -> None:
    """Entry point for the `wiki` console script (placeholder)."""
    config.ensure_dirs()
    print(f"llm-wiki v{__version__}")
    print(f"wiki root: {config.WIKI_ROOT}")
    print("CLI not yet implemented — see CLAUDE.md for the planned commands.")


if __name__ == "__main__":
    main()
