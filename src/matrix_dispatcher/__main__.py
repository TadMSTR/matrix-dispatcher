"""Console-script / ``python -m matrix_dispatcher`` entry point.

Wraps the async :func:`matrix_dispatcher.app.main` in a synchronous callable so
it works as a ``[project.scripts]`` console script, and handles the one-shot
``--cleanup`` retention invocation.
"""

from __future__ import annotations

import asyncio
import sys

from .app import cli_cleanup
from .app import main as _async_main


def main() -> None:
    if "--cleanup" in sys.argv:
        sys.exit(cli_cleanup())
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
