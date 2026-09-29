"""
This module provides the decision engine behind the ``glsrv`` HTTP service. It
evaluates gitignore re-inclusion (allowed/denied) for paths under a repository
root using :class:`pathspec.gitignore.GitIgnoreSpec`, and caches decisions
without ever changing them.
"""
from __future__ import annotations

import os
import os.path
import threading
from dataclasses import (
	dataclass)
from typing import (
	Optional,  # Replaced by `X | None` in 3.10.
	Union)  # Replaced by `X | Y` in 3.10.

from pathspec.gitignore import (
	GitIgnoreSpec)
from pathspec.patterns.gitignore.spec import (
	GitIgnoreSpecPattern)
from pathspec.util import (
	StrPath,
	normalize_file)


@dataclass(frozen=True)
class RuleHit:
	"""
	The :class:`RuleHit` class describes the gitignore rule which decided a
	path.
	"""

	index: int
	"""
	*index* (:class:`int`) is the 0-based index of the pattern in the compiled
	spec.
	"""

	line: int
	"""
	*line* (:class:`int`) is the 1-based line number in the *.gitignore* file.
	"""

	pattern: str
	"""
	*pattern* (:class:`str`) is the uncompiled pattern line.
	"""

	include: bool
	"""
	*include* (:class:`bool`) is whether the pattern ignores (:data:`True`) or
	re-includes/negates (:data:`False`) the path.
	"""


@dataclass(frozen=True)
class Decision:
	"""
	The :class:`Decision` class is the immutable verdict for a single path. It
	is safe to share between threads and to store in cache snapshots.
	"""

	path: str
	"""
	*path* (:class:`str`) is the normalized path. Directories carry a trailing
	``"/"``.
	"""

	is_dir: bool
	"""
	*is_dir* (:class:`bool`) is whether the path is a directory.
	"""

	denied: bool
	"""
	*denied* (:class:`bool`) is whether the path is ignored by the gitignore
	rules.
	"""

	hit: Optional[RuleHit]
	"""
	*hit* (:class:`RuleHit` or :data:`None`) is the rule which decided the
	path, or :data:`None` when no rule matched.
	"""

	@property
	def verdict(self) -> str:
		"""
		Returns the verdict (:class:`str`): ``"denied"`` or ``"allowed"``.
		"""
		return 'denied' if self.denied else 'allowed'


class _DirCache:
	"""
	The :class:`_DirCache` class caches decisions grouped by parent directory.
	Buckets are never mutated in place: writers install a complete replacement
	snapshot under a lock, so concurrent readers always observe a fully formed
	snapshot and never a half-updated intermediate state. Keys include
	*is_dir* so a file and directory with the same name cannot pollute each
	other.
	"""

	def __init__(self) -> None:
		self._lock = threading.Lock()
		self._buckets: dict[str, dict[tuple[str, bool], Decision]] = {}

	@staticmethod
	def _bucket_key(norm_path: str) -> str:
		"""
		Returns the parent directory of the normalized path.
		"""
		parent = norm_path.rstrip('/')
		parent, _sep, _name = parent.rpartition('/')
		return parent

	def get(
		self,
		norm_path: str,
		is_dir: bool,
	) -> Optional[Decision]:
		"""
		Returns the cached :class:`Decision` for the normalized path, or
		:data:`None`. The bucket reference is read atomically; the returned
		snapshot is always complete.
		"""
		bucket = self._buckets.get(self._bucket_key(norm_path))
		if bucket is None:
			return None

		return bucket.get((norm_path, is_dir))

	def put(
		self,
		norm_path: str,
		is_dir: bool,
		decision: Decision,
	) -> None:
		"""
		Stores *decision* under the ``(norm_path, is_dir)`` key by swapping in a
		complete copy of the parent directory bucket.
		"""
		key = self._bucket_key(norm_path)
		with self._lock:
			old = self._buckets.get(key)
			new = dict(old) if old is not None else {}
			new[(norm_path, is_dir)] = decision
			self._buckets[key] = new

	def snapshot(self, norm_path: str) -> dict[tuple[str, bool], Decision]:
		"""
		Returns the current immutable snapshot of the bucket containing
		*norm_path*. Intended for tests and diagnostics.
		"""
		bucket = self._buckets.get(self._bucket_key(norm_path))
		return dict(bucket) if bucket is not None else {}

	def clear(self) -> None:
		"""
		Drops all cached decisions. Subsequent results are recomputed and are
		guaranteed to be identical: the cache only accelerates, it never
		changes conclusions.
		"""
		with self._lock:
			self._buckets = {}


class GitIgnoreEngine:
	"""
	The :class:`GitIgnoreEngine` class answers gitignore allowed/denied
	decisions for paths under a repository root. It does not prune excluded
	directories early: candidate paths are descended into and evaluated
	individually so negation rules can re-include paths.
	"""

	def __init__(self, root: StrPath) -> None:
		"""
		Initializes the engine from the ``.gitignore`` file in *root*.
		"""
		self.root = os.fspath(root)
		"""
		*root* (:class:`str`) is the repository root directory.
		"""

		gitignore_file = os.path.join(self.root, '.gitignore')
		self._lines: list[tuple[int, str]] = []
		patterns: list[GitIgnoreSpecPattern] = []
		with open(gitignore_file, encoding='utf-8') as fp:
			for lineno, line in enumerate(fp.read().splitlines(), 1):
				if line:
					# Mirror `PathSpec.from_lines()` which skips empty lines.
					self._lines.append((lineno, line))
					patterns.append(GitIgnoreSpecPattern(line))

		self.spec = GitIgnoreSpec(patterns)
		"""
		*spec* (:class:`.GitIgnoreSpec`) is the compiled gitignore spec.
		"""

		self._cache = _DirCache()

	@property
	def rules(self) -> list[tuple[int, str]]:
		"""
		Returns the compiled rules as ``(line_number, pattern_text)`` pairs.
		"""
		return list(self._lines)

	def clear_cache(self) -> None:
		"""
		Clears the decision cache. Results are unchanged, only recomputed.
		"""
		self._cache.clear()

	def _hit(self, index: Optional[int]) -> Optional[RuleHit]:
		"""
		Maps a matched pattern index to its source rule.
		"""
		if index is None:
			return None

		lineno, text = self._lines[index]
		include = bool(self.spec.patterns[index].include)
		return RuleHit(index=index, line=lineno, pattern=text, include=include)

	def normalize(self, path: StrPath, is_dir: Optional[bool] = None) -> tuple[str, bool]:
		"""
		Normalizes *path* with :func:`pathspec.util.normalize_file` and
		determines whether it is a directory. A trailing slash marks a
		directory; otherwise the file-system under the root is consulted, and
		finally the explicit *is_dir* hint wins when given. Directories are
		returned with a trailing ``"/"``.
		"""
		norm = normalize_file(path)
		if is_dir is None:
			if norm.endswith('/'):
				is_dir = True
			elif norm:
				native = norm.replace('/', os.sep)
				is_dir = os.path.isdir(os.path.join(self.root, native))
			else:
				is_dir = True

		if is_dir:
			if norm and not norm.endswith('/'):
				norm += '/'
		elif norm.endswith('/'):
			norm = norm.rstrip('/')

		return norm, is_dir

	def check(self, path: StrPath, is_dir: Optional[bool] = None) -> Decision:
		"""
		Returns the :class:`Decision` for *path*. The conclusion is identical
		to :meth:`pathspec.gitignore.GitIgnoreSpec.check_file` and
		:meth:`pathspec.pathspec.PathSpec.match_file` for the same normalized
		path.
		"""
		norm, use_dir = self.normalize(path, is_dir)

		cached = self._cache.get(norm, use_dir)
		if cached is not None:
			return cached

		result = self.spec.check_file(norm)
		decision = Decision(
			path=norm,
			is_dir=use_dir,
			denied=bool(result.include),
			hit=self._hit(result.index),
		)
		self._cache.put(norm, use_dir, decision)
		return decision

	def explain(
		self,
		path: StrPath,
		is_dir: Optional[bool] = None,
	) -> tuple[list[Decision], Decision]:
		"""
		Descends into *path* segment by segment without pruning excluded
		directories, evaluating every ancestor before the final negation-aware
		verdict. Returns the chain of ancestor decisions and the final
		decision.
		"""
		norm, use_dir = self.normalize(path, is_dir)

		parts = norm.rstrip('/').split('/') if norm else []
		chain: list[Decision] = []
		prefix = ''
		for index, part in enumerate(parts):
			prefix += part
			if index == len(parts) - 1:
				chain.append(self.check(prefix, use_dir))
			else:
				chain.append(self.check(prefix, True))
			prefix += '/'

		if chain:
			final = chain[-1]
		else:
			# The root itself is never ignored.
			final = Decision(path='', is_dir=True, denied=False, hit=None)

		return chain, final
