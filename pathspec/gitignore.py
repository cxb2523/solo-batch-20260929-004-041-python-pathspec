"""
This module provides :class:`.GitIgnoreSpec` which replicates *.gitignore*
behavior, and handles edge-cases where Git's behavior differs from what's
documented. Git allows including files from excluded directories which directly
contradicts the documentation. This uses :class:`.GitIgnoreSpecPattern` to fully
replicate Git's handling.
"""
from __future__ import annotations

from collections.abc import (
	Collection,
	Iterable,
	Iterator,
	Sequence)
from dataclasses import (
	dataclass)
from typing import (
	Callable,  # Replaced by `collections.abc.Callable` in 3.9.2.
	Generic,
	Optional,  # Replaced by `X | None` in 3.10.
	TypeVar,
	Union,  # Replaced by `X | Y` in 3.10.
	cast,
	overload)

from pathspec.backend import (
	BackendNamesHint,
	_Backend,
	_TestBackendFactoryHint)
from pathspec._backends.agg import (
	make_gitignore_backend)
from pathspec.pathspec import (
	PathSpec)
from pathspec.pattern import (
	Pattern)
from pathspec.patterns.gitignore.basic import (
	GitIgnoreBasicPattern)
from pathspec.patterns.gitignore.spec import (
	GitIgnoreSpecPattern)
from pathspec._typing import (
	AnyStr,  # Removed in 3.18.
	override)  # Added in 3.12.
from pathspec.util import (
	CheckResult,
	_is_iterable,
	normalize_file,
	TStrPath,
	lookup_pattern)

Self = TypeVar("Self", bound='GitIgnoreSpec')
"""
:class:`.GitIgnoreSpec` self type hint to support Python v<3.11 using PEP 673
recommendation.
"""


@dataclass(frozen=True)
class DrillDownLayer:
	"""
	The :class:`DrillDownLayer` class pairs a compiled :class:`.GitIgnoreSpec`
	with the POSIX path of the directory the patterns were read from. It is used
	with :func:`check_drilldown` to replicate a repository tree containing a
	``.gitignore`` file in each directory.

	*dir_path* (:class:`str`) is the POSIX path of the directory relative to the
	repository root. The root directory is the empty :class:`str`.

	*spec* (:class:`.GitIgnoreSpec`) is the compiled gitignore-spec for the
	``.gitignore`` file in *dir_path*.
	"""

	__slots__ = (
		'dir_path',
		'spec',
	)

	dir_path: str
	spec: GitIgnoreSpec

	def __post_init__(self) -> None:
		dir_path = normalize_file(self.dir_path)
		if dir_path:
			dir_path += '/'
		object.__setattr__(self, 'dir_path', dir_path)


@dataclass(frozen=True)
class DrillDownResult(Generic[TStrPath]):
	"""
	The :class:`DrillDownResult` class contains the result of checking a file
	against a stack of nested ``.gitignore`` files with :func:`check_drilldown`.

	*file* (:class:`str` or :class:`os.PathLike`) is the original file path.

	*include* (:class:`bool` or :data:`None`) is whether the file is ignored
	(:data:`True`), re-included (:data:`False`), or unmatched (:data:`None`).

	*index* (:class:`int` or :data:`None`) is the index of the matched pattern
	in the matched layer's :class:`.GitIgnoreSpec`.

	*layer* (:class:`DrillDownLayer` or :data:`None`) is the deepest layer
	containing a matching pattern.
	"""

	__slots__ = (
		'file',
		'include',
		'index',
		'layer',
	)

	file: TStrPath
	include: Optional[bool]
	index: Optional[int]
	layer: Optional[DrillDownLayer]


def check_drilldown(
	layers: Sequence[DrillDownLayer],
	file: TStrPath,
	separators: Optional[Collection[str]] = None,
	*,
	is_dir: Optional[bool] = None,
) -> DrillDownResult[TStrPath]:
	"""
	Checks *file* against nested ``.gitignore`` files using Git's drill-down
	rule: candidate paths are descended into even when an ancestor directory is
	excluded, and a negation in a deeper ``.gitignore`` re-includes the path.

	*layers* (:class:`~collections.abc.Sequence` of :class:`DrillDownLayer`)
	contains the compiled layers ordered from the root directory to the deepest
	ancestor directory of *file*. Each layer's patterns only apply to paths
	beneath its directory.

	*file* (:class:`str` or :class:`os.PathLike`) is the file path relative to
	the repository root.

	*separators* (:class:`~collections.abc.Collection` of :class:`str`; or
	:data:`None`) optionally contains the path separators to normalize. See
	:func:`.normalize_file` for more information.

	*is_dir* (:class:`bool` or :data:`None`) optionally indicates whether
	*file* is a directory.

	Returns the deepest layer that matches (:class:`DrillDownResult`). If no
	layer matches, the result's :attr:`~.DrillDownResult.include` and
	:attr:`~.DrillDownResult.index` attributes are :data:`None`.
	"""
	norm_file = normalize_file(file, separators, is_dir=is_dir)

	out_result: DrillDownResult[TStrPath] = DrillDownResult(file, None, None, None)

	for layer in layers:
		layer_prefix = layer.dir_path
		if not norm_file.startswith(layer_prefix):
			continue

		rel_file = norm_file[len(layer_prefix):]
		if not rel_file or rel_file == '/':
			# A .gitignore file does not apply to the directory containing it.
			continue

		# Evaluate through the same public entry point as direct calls so the
		# relative path reaches the same conclusion. Separators are disabled
		# because *rel_file* is already normalized POSIX.
		check = layer.spec.check_file(rel_file, (), is_dir=is_dir)
		if check.include is not None:
			out_result = DrillDownResult(
				file, check.include, check.index, layer,
			)

	return out_result


def check_drilldown_files(
	layers: Sequence[DrillDownLayer],
	files: Iterable[TStrPath],
	separators: Optional[Collection[str]] = None,
	*,
	is_dir: Optional[bool] = None,
) -> Iterator[DrillDownResult[TStrPath]]:
	"""
	Checks each file with :func:`check_drilldown`.

	*layers* (:class:`~collections.abc.Sequence` of :class:`DrillDownLayer`)
	contains the compiled layers ordered from root to deepest.

	*files* (:class:`~collections.abc.Iterable` of :class:`str` or
	:class:`os.PathLike`) contains the file paths to check.

	*separators* (:class:`~collections.abc.Collection` of :class:`str`; or
	:data:`None`) optionally contains the path separators to normalize.

	*is_dir* (:class:`bool` or :data:`None`) optionally indicates whether each
	file is a directory.

	Returns an :class:`~collections.abc.Iterator` yielding each
	:class:`DrillDownResult`.
	"""
	if not _is_iterable(files):
		raise TypeError(f"files:{files!r} is not an iterable.")

	for file in files:
		yield check_drilldown(
			layers, file, separators, is_dir=is_dir,
		)


class GitIgnoreSpec(PathSpec[GitIgnoreSpecPattern]):
	"""
	The :class:`GitIgnoreSpec` class extends :class:`.PathSpec` to replicate
	*gitignore* behavior. This is uses :class:`.GitIgnoreSpecPattern` to fully
	replicate Git's handling.
	"""

	def __eq__(self, other: object) -> bool:
		"""
		Tests the equality of this gitignore-spec with *other* (:class:`.GitIgnoreSpec`)
		by comparing their :attr:`self.patterns <.PathSpec.patterns>` attributes. A
		non-:class:`GitIgnoreSpec` will not compare equal.
		"""
		if isinstance(other, GitIgnoreSpec):
			return super().__eq__(other)
		elif isinstance(other, PathSpec):
			return False
		else:
			return NotImplemented

	@override
	def check_file(
		self,
		file: TStrPath,
		separators: Optional[Collection[str]] = None,
		*,
		is_dir: Optional[bool] = None,
	) -> CheckResult[TStrPath]:
		"""
		Check *file* against this gitignore-spec.

		*file* (:class:`str` or :class:`os.PathLike`) is the file path to be
		matched against :attr:`self.patterns <.PathSpec.patterns>`.

		*separators* (:class:`~collections.abc.Collection` of :class:`str`; or
		:data:`None`) optionally contains the path separators to normalize. See
		:func:`.normalize_file` for more information.

		*is_dir* (:class:`bool` or :data:`None`) optionally indicates whether
		*file* is a directory, ensuring directory-only patterns are evaluated with
		the trailing path separator and file-only patterns without one.

		Returns the file check result (:class:`.CheckResult`).
		"""
		return super().check_file(file, separators, is_dir=is_dir)

	@override
	def check_files(
		self,
		files: Iterable[TStrPath],
		separators: Optional[Collection[str]] = None,
		*,
		is_dir: Optional[bool] = None,
	) -> Iterator[CheckResult[TStrPath]]:
		"""
		Check the files against this gitignore-spec.

		*files* (:class:`~collections.abc.Iterable` of :class:`str` or
		:class:`os.PathLike`) contains the file paths to be checked.

		*separators* (:class:`~collections.abc.Collection` of :class:`str`; or
		:data:`None`) optionally contains the path separators to normalize. See
		:func:`.normalize_file` for more information.

		*is_dir* (:class:`bool` or :data:`None`) optionally indicates whether
		each file is a directory.

		Returns an :class:`~collections.abc.Iterator` yielding each file check
		result (:class:`.CheckResult`).
		"""
		yield from super().check_files(files, separators, is_dir=is_dir)

	# Support reversed order of arguments from PathSpec.
	@overload  # type: ignore[override]
	@classmethod
	def from_lines(
		cls: type[Self],
		pattern_factory: Union[str, type[Pattern], Callable[[AnyStr], Pattern], None],
		lines: Iterable[AnyStr],
		*,
		backend: Union[BackendNamesHint, str, None] = None,
		_test_backend_factory: _TestBackendFactoryHint = None,
	) -> Self:
		...

	@overload
	@classmethod
	def from_lines(
		cls: type[Self],
		lines: Iterable[AnyStr],
		pattern_factory: Union[str, type[Pattern], Callable[[AnyStr], Pattern], None] = None,
		*,
		backend: Union[BackendNamesHint, str, None] = None,
		_test_backend_factory: _TestBackendFactoryHint = None,
	) -> Self:
		...

	@override  # type: ignore[misc]
	@classmethod
	def from_lines(  # type: ignore
		cls: type[Self],
		lines: Iterable[AnyStr],
		pattern_factory: Union[str, type[Pattern], Callable[[AnyStr], Pattern], None] = None,
		*,
		backend: Union[BackendNamesHint, str, None] = None,
		_test_backend_factory: _TestBackendFactoryHint = None,
	) -> Self:
		"""
		Compiles the pattern lines.

		*lines* (:class:`~collections.abc.Iterable`) yields each uncompiled pattern
		(:class:`str`). This simply has to yield each line, so it can be a
		:class:`io.TextIOBase` (e.g., from :func:`open` or :class:`io.StringIO`) or
		the result from :meth:`str.splitlines`.

		*pattern_factory* does not need to be set for :class:`GitIgnoreSpec`. If
		set, it should be either ``"gitignore"`` or :class:`.GitIgnoreSpecPattern`.
		There is no guarantee it will work with any other pattern class. Default is
		:data:`None` for :class:`.GitIgnoreSpecPattern`.

		*backend* (:class:`str` or :data:`None`) is the pattern (regular expression)
		matching backend to use. Default is :data:`None` for "best" to use the best
		available backend. Priority of backends is: "re2", "hyperscan", "simple".
		The "simple" backend is always available.

		Returns the :class:`GitIgnoreSpec` instance.
		"""
		if (isinstance(lines, (str, bytes)) or callable(lines)) and _is_iterable(pattern_factory):
			# Support reversed order of arguments from PathSpec.
			pattern_factory, lines = lines, pattern_factory  # type: ignore

		use_factory: Callable[[AnyStr], GitIgnoreSpecPattern]
		if pattern_factory is None:
			use_factory = GitIgnoreSpecPattern  # type: ignore[assignment]
		elif pattern_factory == 'gitignore':
			# Force use of GitIgnoreSpecPattern for "gitignore" to handle edge-cases.
			# This makes usage easier.
			use_factory = GitIgnoreSpecPattern  # type: ignore[assignment]
		elif isinstance(pattern_factory, str):
			use_factory = lookup_pattern(pattern_factory)  # type: ignore[assignment]
		else:
			use_factory = pattern_factory  # type: ignore[assignment]

		if (
			isinstance(use_factory, type)
			and issubclass(use_factory, GitIgnoreBasicPattern)
		):
			raise TypeError((
				f"pattern_factory={pattern_factory!r} (resolved to {use_factory}) "
				f"cannot be {GitIgnoreBasicPattern} because it will give unexpected "
				f"results."
			))  # TypeError

		self = super().from_lines(use_factory, lines, backend=backend, _test_backend_factory=_test_backend_factory)  # type: ignore[arg-type,type-var]
		return self  # type: ignore[return-value]

	@override
	@staticmethod
	def _make_backend(
		name: BackendNamesHint,
		patterns: Sequence[Pattern],
	) -> _Backend:
		"""
		.. warning:: This method is not part of the public API. It is subject to
			change.

		Create the backend for the patterns.

		*name* (:class:`str`) is the name of the backend.

		*patterns* (:class:`~collections.abc.Sequence` of :class:`.Pattern`)
		contains the compiled patterns.

		Returns the backend (:class:`._Backend`).
		"""
		return make_gitignore_backend(name, patterns)
