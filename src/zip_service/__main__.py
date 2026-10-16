"""``python -m zip_service``."""

from __future__ import annotations

import sys

from zip_service.server import main

if __name__ == "__main__":
    sys.exit(main())
