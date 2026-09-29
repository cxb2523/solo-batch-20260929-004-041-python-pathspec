"""
The :mod:`glsrv` package provides a resident HTTP service for checking paths
against the ``.gitignore`` files in a repository.

Start the service with::

	python -m glsrv --root <repo>

Then request::

	GET /check?path=<path>

The response reports whether the path is allowed or denied (ignored), the
	pattern that decided it, and the file and line number of that pattern.

The service uses the drill-down rule: candidate paths are descended into even
when an ancestor directory is excluded, so a negation in a deeper
``.gitignore`` file can re-include a path.
"""

from .policy import (
	IgnorePolicy,
	LayerView,
	PolicyError,
	RuleHit)

__all__ = [
	'IgnorePolicy',
	'LayerView',
	'PolicyError',
	'RuleHit',
]
