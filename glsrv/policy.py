"""
This module provides the in-process policy engine backing the resident
``glsrv`` HTTP service.

The engine discovers each ``.gitignore`` file along a queried path, compiles it
with :class:`.GitIgnoreSpec`, and applies the drill-down rule:

-	Excluded ancestor directories are not pruned. Every candidate directory is
	descended into so a negation in a deeper ``.gitignore`` file can
	re-include the path. The deepest layer with a match decides.
-	Files and directories are cached separately (the cache key includes
	``is_dir``), so a file and a directory sharing a name can never pollute each
	other.
-	Directory snapshots and decisions are immutable once published. Cache writes
	swap complete snapshots atomically, so concurrent readers never observe a
	half-updated state.
-	The cache only accelerates. Every cached entry is validated against the
	file-system state (mtime/size/name) of its ``.gitignore`` files; a fresh
	evaluation would produce the same result after a restart.
"""
from __future__ import annotations

import os
import os.path
import threading
from dataclasses import (
	dataclass)
from typing import (
	Optional)

from pathspec.gitignore import (
	DrillDownLayer,
	GitIgnoreSpec,
	check_drilldown)
from pathspec.util import (
	StrPath,
	normalize_file)

GITIGNORE_NAME = '.gitignore'
"""
Name of the ignore file loaded from each directory.
"""


class PolicyError(Exception):
	"""
	The :exc:`PolicyError` exception is raised when a request cannot be
	evaluated (e.g., the path escapes the repository root).
	"""


@dataclass(frozen=True)
class RuleHit:
	"""
	The :class:`RuleHit` class identifies the pattern that decided a path.
	"""

	__slots__ = (
		'action',
		'line',
		'pattern',
		'source',
	)

	source: str
	"""
	*source* (:class:`str`) is the POSIX path of the ``.gitignore`` file
	containing the pattern, relative to the repository root.
	"""

	line: int
	"""
	*line* (:class:`int`) is the one-based line number of the pattern in
	:attr:`source`.
	"""

	pattern: str
	"""
	*pattern* (:class:`str`) is the raw, uncompiled pattern line.
	"""

	action: str
	"""
	*action* (:class:`str`) is ``"ignore"`` when the pattern excludes the path
	or ``"unignore"`` when the pattern negates an earlier exclusion.
	"""

	def to_dict(self) -> dict[str, object]:
		"""
		Serialize the hit to a plain :class:`dict`.
		"""
		return {
			'source': self.source,
			'line': self.line,
			'pattern': self.pattern,
			'action': self.action,
		}


@dataclass(frozen=True)
class LayerView:
	"""
	The :class:`LayerView` class describes a ``.gitignore`` layer considered
	while evaluating a path.
	"""

	__slots__ = (
		'matched',
		'source',
	)

	source: str
	"""
	*source* (:class:`str`) is the POSIX path of the ``.gitignore`` file.
	"""

	matched: bool
	"""
	*matched* (:class:`bool`) is whether this layer contained the deciding
	pattern.
	"""

	def to_dict(self) -> dict[str, object]:
		"""
		Serialize the layer to a plain :class:`dict`.
		"""
		return {
			'source': self.source,
			'matched': self.matched,
		}


@dataclass(frozen=True)
class Decision:
	"""
	The :class:`Decision` class is the immutable result of evaluating a path.
	"""

	__slots__ = (
		'ignored',
		'is_dir',
		'layers',
		'path',
		'rule',
	)

	path: str
	"""
	*path* (:class:`str`) is the normalized POSIX path queried, without a
	trailing separator.
	"""

	is_dir: bool
	"""
	*is_dir* (:class:`bool`) is whether the path was evaluated as a directory.
	"""

	ignored: Optional[bool]
	"""
	*ignored* (:class:`bool` or :data:`None`) is :data:`True` when the path is
	ignored, :data:`False` when it is re-included by a negation, and
	:data:`None` when no pattern matched.
	"""

	rule: Optional[RuleHit]
	"""
	*rule* (:class:`RuleHit` or :data:`None`) is the deciding pattern.
	"""

	layers: tuple[LayerView, ...]
	"""
	*layers* (:class:`tuple` of :class:`LayerView`) lists the ``.gitignore``
	files considered, in root-to-deepest order.
	"""

	@property
	def verdict(self) -> str:
		"""
		*verdict* (:class:`str`) is ``"denied"`` when the path is ignored,
		otherwise ``"allowed"``.
		"""
		return 'denied' if self.ignored else 'allowed'

	def to_dict(self) -> dict[str, object]:
		"""
		Serialize the decision to a plain :class:`dict`.
		"""
		return {
			'path': self.path,
			'is_dir': self.is_dir,
			'verdict': self.verdict,
			'ignored': self.ignored,
			'rule': None if self.rule is None else self.rule.to_dict(),
			'layers': [layer.to_dict() for layer in self.layers],
		}


@dataclass(frozen=True)
class _LayerSnapshot:
	"""
	The :class:`_LayerSnapshot` class is an immutable, compiled ``.gitignore``
	file.
	"""

	__slots__ = (
		'lines',
		'pattern_lines',
		'signature',
		'source',
		'spec',
	)

	source: str
	signature: tuple[object, ...]
	lines: tuple[str, ...]
	pattern_lines: tuple[int, ...]
	spec: GitIgnoreSpec


_EMPTY_CHAIN: tuple[Optional[_LayerSnapshot], ...] = ()
_EMPTY_SIGNATURE: tuple[tuple[object, ...], ...] = ()


@dataclass(frozen=True)
class _DirSnapshot:
	"""
	The :class:`_DirSnapshot` class is an immutable view of the ``.gitignore``
	files along a directory chain.
	"""

	__slots__ = (
		'chain',
		'signature',
	)

	chain: tuple[Optional[_LayerSnapshot], ...]
	signature: tuple[tuple[object, ...], ...]


_EMPTY_DIR_SNAPSHOT = _DirSnapshot(_EMPTY_CHAIN, _EMPTY_SIGNATURE)


@dataclass(frozen=True)
class _CacheEntry:
	"""
	The :class:`_CacheEntry` class pairs an immutable :class:`Decision` with
	the directory-chain signature it was computed from. The decision is reused
	only while its signature still matches the file-system.
	"""

	__slots__ = (
		'decision',
		'signature',
	)

	decision: Decision
	signature: tuple[tuple[object, ...], ...]


class IgnorePolicy:
	"""
	The :class:`IgnorePolicy` class evaluates repository paths against the
	``.gitignore`` files using the drill-down rule.

	*root* (:class:`str` or :class:`os.PathLike`) is the repository root
	directory.
	"""

	def __init__(self, root: StrPath) -> None:
		self.root: str = os.path.abspath(os.fspath(root))
		"""
		*root* (:class:`str`) is the absolute repository root directory.
		"""

		if not os.path.isdir(self.root):
			raise NotADirectoryError(f"root:{self.root!r} is not a directory.")

		self._lock = threading.RLock()
		"""
		*_lock* (:class:`threading.RLock`) guards the immutable snapshot
		caches. All expensive file-system work and matching happens outside the
		lock; only atomic dictionary publication is serialized.
		"""

		self._dir_cache: dict[str, _DirSnapshot] = {}
		"""
		*_dir_cache* (:class:`dict`) maps each ancestor directory POSIX path
		(:class:`str`) to its immutable :class:`_DirState`.
		"""

		self._chain_cache: dict[str, _DirSnapshot] = {}
		"""
		*_chain_cache* (:class:`dict`) maps each ancestor directory POSIX path
		(:class:`str`) to the immutable :class:`_DirSnapshot` of the complete
		root-to-directory ``.gitignore`` chain. Each entry is published in one
		assignment, so readers only ever see complete chains.
		"""

		self._decision_cache: dict[tuple[str, bool], _CacheEntry] = {}
		"""
		*_decision_cache* (:class:`dict`) maps each ``(path, is_dir)`` key
		(:class:`tuple`) to its immutable :class:`Decision`. The key includes
		``is_dir`` so a file and directory sharing a name never share an entry.
		"""

		self._hits = 0
		self._misses = 0

	@property
	def stats(self) -> dict[str, int]:
		"""
		*stats* (:class:`dict`) reports cache hit and miss counters.
		"""
		with self._lock:
			return {
				'hits': self._hits,
				'misses': self._misses,
				'directories': len(self._dir_cache),
				'chains': len(self._chain_cache),
				'decisions': len(self._decision_cache),
			}

	def invalidate(self) -> None:
		"""
		Clears all cached snapshots and decisions. This does not change any
		result; subsequent requests are evaluated from scratch.
		"""
		with self._lock:
			self._dir_cache.clear()
			self._chain_cache.clear()
			self._decision_cache.clear()

	def check(
		self,
		path: StrPath,
		*,
		is_dir: Optional[bool] = None,
		refresh: Optional[bool] = None,
	) -> Decision:
		"""
		Evaluate *path* against the ``.gitignore`` files in the repository.

		*path* (:class:`str` or :class:`os.PathLike`) is the path relative to
		the repository root. Absolute paths must be inside the root.

		*is_dir* (:class:`bool` or :data:`None`) optionally forces whether the
		path is a directory. When :data:`None`, a trailing separator or the
		file-system is used to determine it.

		*refresh* (:class:`bool` or :data:`None`) ignores cached decisions when
		:data:`True`.

		Returns the immutable :class:`Decision`.

		Raises :exc:`PolicyError` if *path* escapes the repository root.
		"""
		rel_path, is_dir = self._resolve(path, is_dir)

		key = (rel_path, is_dir)

		if not refresh:
			with self._lock:
				cached = self._decision_cache.get(key)

			if cached is not None:
				if self._is_chain_current(rel_path, cached.signature):
					with self._lock:
						self._hits += 1
					return cached.decision

		with self._lock:
			self._misses += 1

		snapshot = self._get_dir_snapshot(rel_path, refresh=True)
		decision = self._evaluate(rel_path, is_dir, snapshot)

		# Publish the complete immutable decision atomically. Readers holding a
		# reference to the previous decision always observe a complete object.
		with self._lock:
			self._decision_cache[key] = _CacheEntry(decision, snapshot.signature)

		return decision

	def _resolve(
		self,
		path: StrPath,
		is_dir: Optional[bool],
	) -> tuple[str, bool]:
		"""
		Resolve *path* to a POSIX path relative to the root and determine
		whether it is a directory.

		Returns a :class:`tuple` containing the relative path (:class:`str`)
		without a trailing separator and whether it is a directory
		(:class:`bool`).
		"""
		raw = os.fspath(path)
		trailing = raw.endswith('/') or (
			os.altsep is not None and raw.endswith(os.altsep)
		)

		if os.path.isabs(raw):
			abs_path = os.path.abspath(raw)
		else:
			abs_path = os.path.abspath(os.path.join(self.root, raw))

		root_norm = os.path.normcase(self.root)
		abs_norm = os.path.normcase(abs_path)
		try:
			inside = (
				abs_norm == root_norm
				or os.path.commonpath((root_norm, abs_norm)) == root_norm
			)
		except ValueError:
			inside = False
		if not inside:
			raise PolicyError(f"path:{raw!r} escapes root:{self.root!r}.")

		rel = os.path.relpath(abs_path, self.root)
		if os.path.normcase(rel) == os.curdir:
			rel = ''

		rel_posix = normalize_file(rel)

		if is_dir is None:
			if trailing:
				is_dir = True
			elif os.path.exists(abs_path):
				is_dir = os.path.isdir(abs_path)
			else:
				is_dir = False

		return (rel_posix, is_dir)

	def _is_chain_current(
		self,
		rel_path: str,
		signature: tuple[tuple[object, ...], ...],
	) -> bool:
		"""
		Check whether the ancestor chain of *rel_path* still matches
		*signature*. Each ``.gitignore`` file is statted; an unchanged file
		avoids recompilation and matching entirely.
		"""
		dirs = self._ancestor_dirs(rel_path)
		if len(dirs) != len(signature):
			return False

		for dir_path, expected in zip(dirs, signature):
			dir_full = (
				os.path.join(self.root, *dir_path.split('/'))
				if dir_path else self.root
			)
			ignore_full = os.path.join(dir_full, GITIGNORE_NAME)
			current = self._stat_signature(dir_full, ignore_full)
			if current != expected:
				return False

			# Refresh the cached directory state's verification without
			# recompiling the ignore file.
			state = self._get_dir_state(dir_path)
			if state.signature != expected:
				return False

		return True

	@staticmethod
	def _ancestor_dirs(rel_path: str) -> tuple[str, ...]:
		"""
		Get the ancestor directories of *rel_path*, as POSIX paths relative to
		the root, ordered root-to-parent. The root is the empty :class:`str`.
		Only ancestor directories contain ``.gitignore`` files that can apply to
		the path; a directory's own ``.gitignore`` does not apply to itself.
		"""
		if not rel_path:
			return ()

		segments = rel_path.split('/')[:-1]
		out: list[str] = ['']
		current = ''
		for segment in segments:
			if current:
				current += '/'
			current += segment
			out.append(current)

		return tuple(out)

	def _get_dir_snapshot(
		self,
		rel_path: str,
		*,
		refresh: Optional[bool] = None,
	) -> _DirSnapshot:
		"""
		Get an immutable snapshot of the ``.gitignore`` files along the ancestor
		directory chain of *rel_path*.

		*refresh* (:class:`bool` or :data:`None`) bypasses cached directory
		states when :data:`True`.

		Returns the immutable :class:`_DirSnapshot`.
		"""
		dirs = self._ancestor_dirs(rel_path)

		if not dirs:
			return _EMPTY_DIR_SNAPSHOT

		# Walk root-to-deepest, reusing the longest valid cached chain prefix.
		# Each chain snapshot is immutable and published in one dictionary
		# assignment, so a concurrent reader never sees a partially extended
		# chain.
		chain: list[Optional[_LayerSnapshot]] = []
		signature: list[tuple[object, ...]] = []

		for index, dir_path in enumerate(dirs):
			cached = None
			if not refresh:
				with self._lock:
					cached = self._chain_cache.get(dir_path)

			if (
				cached is not None
				and len(cached.signature) == index + 1
				and tuple(cached.signature[:index]) == tuple(signature)
			):
				state = self._get_dir_state(dir_path)
				if state.signature == cached.signature[index]:
					chain.append(cached.chain[index])
					signature.append(state.signature)
					continue

			state = self._get_dir_state(dir_path, refresh=refresh)
			chain.append(state.layer)
			signature.append(state.signature)
			snapshot = _DirSnapshot(tuple(chain), tuple(signature))
			with self._lock:
				self._chain_cache[dir_path] = snapshot

		return _DirSnapshot(tuple(chain), tuple(signature))

	def _get_dir_state(
		self,
		dir_path: str,
		*,
		refresh: Optional[bool] = None,
	) -> '_DirState':
		"""
		Get the immutable state of a single ancestor directory.

		*dir_path* (:class:`str`) is the POSIX path of the directory relative to
		the root (``""`` for the root).

		*refresh* (:class:`bool` or :data:`None`) bypasses the cache.

		Returns the immutable :class:`_DirState`. Publication of each state is a
		single dictionary assignment, so readers never see a partially built
		layer.
		"""
		dir_full = os.path.join(self.root, *dir_path.split('/')) if dir_path else self.root
		ignore_full = os.path.join(dir_full, GITIGNORE_NAME)

		current_sig = self._stat_signature(dir_full, ignore_full)

		if not refresh:
			with self._lock:
				cached = self._dir_cache.get(dir_path)
			if cached is not None and cached.signature == current_sig:
				return cached

		layer: Optional[_LayerSnapshot]
		if not current_sig[0] or current_sig[1] is None:
			layer = None
		else:
			layer = self._load_layer(dir_path, ignore_full)

		state = _DirState(current_sig, layer)
		with self._lock:
			self._dir_cache[dir_path] = state
		return state

	@staticmethod
	def _stat_signature(
		dir_full: str,
		ignore_full: str,
	) -> tuple[object, ...]:
		"""
		Get the file-system signature of an ancestor directory and its
		``.gitignore`` file. The signature has the same shape regardless of
		whether the ignore file exists, so cached states compare cleanly.

		Returns a :class:`tuple` containing whether the directory exists
		(:class:`bool`), and either :data:`None` for a missing ``.gitignore``
		file or its stat signature (:class:`tuple`).
		"""
		if not os.path.isdir(dir_full):
			return (False, None)

		try:
			stat_result = os.stat(ignore_full)
		except OSError:
			return (True, None)

		return (True, (
			stat_result.st_mtime_ns,
		stat_result.st_size,
		stat_result.st_ino,
		stat_result.st_dev,
	))

	def _load_layer(
		self,
		dir_path: str,
		ignore_full: str,
	) -> _LayerSnapshot:
		"""
		Read and compile a single ``.gitignore`` file.

		*dir_path* (:class:`str`) is the POSIX directory path relative to the
		root.

		*ignore_full* (:class:`str`) is the absolute path of the
		``.gitignore`` file.

		Returns the immutable :class:`_LayerSnapshot`. The file is re-statted
		after reading; a concurrent write changes the signature and discards the
		compilation.
		"""
		before = os.stat(ignore_full)
		with open(ignore_full, 'r', encoding='utf-8', errors='replace') as stream:
			text = stream.read()
		after = os.stat(ignore_full)
		if (
			before.st_mtime_ns != after.st_mtime_ns
			or before.st_size != after.st_size
		):
			# The file changed while it was being read. Compile the current
			# contents instead; the signature check will reload next request if
			# the file is still being written.
			return self._load_layer(dir_path, ignore_full)

		lines = tuple(text.splitlines())

		spec_lines = list(lines)
		pattern_lines = tuple(
			index
			for index, line in enumerate(lines)
			if line
		)

		spec = GitIgnoreSpec.from_lines(spec_lines, backend='simple')

		source = (
			GITIGNORE_NAME
			if not dir_path
			else f"{dir_path}/{GITIGNORE_NAME}"
		)

		signature = (
			True,
			(
				after.st_mtime_ns,
				after.st_size,
				after.st_ino,
				after.st_dev,
			),
		)

		return _LayerSnapshot(source, signature, lines, pattern_lines, spec)

	def _evaluate(
		self,
		rel_path: str,
		is_dir: bool,
		snapshot: _DirSnapshot,
	) -> Decision:
		"""
		Evaluate *rel_path* against the compiled layers in *snapshot* using the
		drill-down rule.

		Returns the immutable :class:`Decision`.
		"""
		layers: list[DrillDownLayer] = []
		views: list[LayerView] = []
		for layer in snapshot.chain:
			if layer is None:
				continue
			views.append(LayerView(layer.source, False))
			layers.append(DrillDownLayer(
				os.path.dirname(layer.source),
				layer.spec,
			))

		rule: Optional[RuleHit] = None
		ignored: Optional[bool] = None

		if rel_path:
			result = check_drilldown(
				layers, rel_path, is_dir=is_dir,
			)
			ignored = result.include

			if result.layer is not None and result.index is not None:
				for layer in snapshot.chain:
					if (
						layer is not None
						and layer.source == result.layer.dir_path + GITIGNORE_NAME
					):
						line = layer.pattern_lines[result.index] + 1
						pattern = layer.lines[layer.pattern_lines[result.index]]
						action = 'ignore' if result.include else 'unignore'
						rule = RuleHit(layer.source, line, pattern, action)
						break

		if rule is not None:
			views = [
				LayerView(view.source, view.source == rule.source)
				for view in views
			]

		return Decision(
			rel_path, is_dir, ignored, rule, tuple(views),
		)


@dataclass(frozen=True)
class _DirState:
	"""
	The :class:`_DirState` class is the immutable cached state of a single
	ancestor directory.
	"""

	__slots__ = (
		'layer',
		'signature',
	)

	signature: tuple[object, ...]
	layer: Optional[_LayerSnapshot]
