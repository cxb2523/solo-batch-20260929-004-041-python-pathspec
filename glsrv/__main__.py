"""
This module allows starting the resident HTTP service with
``python -m glsrv --root <repo>``.
"""
from __future__ import annotations

import sys

from .server import (
	main)


if __name__ == '__main__':
	sys.exit(main())
