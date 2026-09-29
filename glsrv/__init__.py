"""
The :mod:`glsrv` package provides a persistent HTTP service which answers
gitignore re-inclusion decisions (allowed/denied) for paths under a repository
root. It wraps :class:`pathspec.gitignore.GitIgnoreSpec` so the service, the
in-process :meth:`~pathspec.gitignore.GitIgnoreSpec.check_file`/
:meth:`~pathspec.gitignore.GitIgnoreSpec.check_files` methods, and
:meth:`~pathspec.pathspec.PathSpec.match_file` all reach the same conclusion
for the same path.
"""
from glsrv.engine import (
	Decision,
	GitIgnoreEngine,
	RuleHit)

__all__ = [
	'Decision',
	'GitIgnoreEngine',
	'RuleHit',
]
