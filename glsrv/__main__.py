"""
Runs the persistent gitignore decision service: ``python -m glsrv --root
<repo>``.
"""
from __future__ import annotations

import argparse
from collections.abc import (
	Sequence)
from typing import (
	Optional)  # Replaced by `X | None` in 3.10.

from glsrv.server import (
	serve)


def main(argv: Optional[Sequence[str]] = None) -> None:
	"""
	Parses the command-line arguments and starts the service.
	"""
	parser = argparse.ArgumentParser(
		prog='glsrv',
		description="Persistent HTTP service for gitignore allowed/denied decisions.",
	)
	parser.add_argument(
		'--root', required=True,
		help="the repository root containing the .gitignore file.",
	)
	parser.add_argument(
		'--host', default='127.0.0.1',
		help="the interface to bind (default: 127.0.0.1).",
	)
	parser.add_argument(
		'--port', type=int, default=8000,
		help="the port to bind (default: 8000).",
	)
	args = parser.parse_args(argv)
	serve(args.root, args.host, args.port)


if __name__ == '__main__':
	main()
