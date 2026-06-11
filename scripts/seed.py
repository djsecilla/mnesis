#!/usr/bin/env python
"""Local wrapper around mnesis.seed — seeds the configured MNESIS_ROOT offline.

In Docker, prefer the `docker-seed` make target (`python -m mnesis.seed` in the
container). Locally: `MNESIS_ROOT=/tmp/mnesis-seed python scripts/seed.py`.
"""

from __future__ import annotations

from mnesis.seed import main

if __name__ == "__main__":
    main()
