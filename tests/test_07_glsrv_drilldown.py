"""
This script tests the drill-down re-inclusion primitives and the shared
``is_dir`` handling used by the resident ``glsrv`` service.
"""
import unittest
from collections.abc import (
	Iterable)

from pathspec.gitignore import (
	DrillDownLayer,
	GitIgnoreSpec,
	check_drilldown,
	check_drilldown_files)
from pathspec.util import (
	normalize_file)


def make_layers(lines_by_dir: dict[str, Iterable[str]]) -> list[DrillDownLayer]:
	"""
	Build drill-down layers from a mapping of POSIX directory path (``""`` for
	root) to gitignore lines.
	"""
	out = []
	for dir_path in sorted(lines_by_dir, key=lambda value: value.count('/')):
		out.append(DrillDownLayer(
			dir_path,
			GitIgnoreSpec.from_lines(lines_by_dir[dir_path], backend='simple'),
		))
	return out


class NormalizeFileIsDirTest(unittest.TestCase):
	"""
	The :class:`NormalizeFileIsDirTest` class tests the ``is_dir`` handling of
	:func:`.normalize_file`.
	"""

	def test_01_none_preserves_trailing_slash(self) -> None:
		"""
		A trailing slash is untouched when ``is_dir`` is not given.
		"""
		self.assertEqual(normalize_file('a/b/'), 'a/b/')
		self.assertEqual(normalize_file('a/b'), 'a/b')

	def test_02_true_adds_trailing_slash(self) -> None:
		"""
		``is_dir=True`` guarantees a single trailing slash.
		"""
		self.assertEqual(normalize_file('a/b', is_dir=True), 'a/b/')
		self.assertEqual(normalize_file('a/b/', is_dir=True), 'a/b/')

	def test_03_false_removes_trailing_slash(self) -> None:
		"""
		``is_dir=False`` removes a single trailing slash.
		"""
		self.assertEqual(normalize_file('a/b/', is_dir=False), 'a/b')
		self.assertEqual(normalize_file('a/b', is_dir=False), 'a/b')


class ThreeEntryPointsConsistencyTest(unittest.TestCase):
	"""
	The :class:`ThreeEntryPointsConsistencyTest` class verifies the same path
	reaches the same conclusion through :meth:`.check_file`,
	:meth:`.check_files`, and :meth:`.match_file`.
	"""

	def test_01_same_conclusion(self) -> None:
		spec = GitIgnoreSpec.from_lines([
			'build/',
			'*.log',
			'!keep.log',
		], backend='simple')

		paths = [
			'build',
			'build/x.txt',
			'build/keep.log',
			'keep.log',
			'a.log',
			'a.txt',
		]

		for path in paths:
			is_dir = path == 'build'

			single = spec.check_file(path, is_dir=is_dir)
			batch = list(spec.check_files([path], is_dir=is_dir))[0]
			matched = spec.match_file(path, is_dir=is_dir)

			self.assertEqual(single.include, batch.include, path)
			self.assertEqual(single.index, batch.index, path)
			self.assertEqual(bool(single.include), matched, path)

	def test_02_explicit_is_dir_matches_trailing_slash(self) -> None:
		"""
		Passing ``is_dir=True`` is equivalent to a trailing slash.
		"""
		spec = GitIgnoreSpec.from_lines(['build/'], backend='simple')

		self.assertEqual(
			spec.check_file('build', is_dir=True).include,
			spec.check_file('build/').include,
		)

		spec = GitIgnoreSpec.from_lines(['*.log'], backend='simple')
		self.assertEqual(
			spec.check_file('a.log', is_dir=False).include,
			spec.check_file('a.log').include,
		)


class DrillDownTest(unittest.TestCase):
	"""
	The :class:`DrillDownTest` class tests :func:`check_drilldown`.
	"""

	def test_01_single_layer_equals_flat_spec(self) -> None:
		"""
		A single root layer produces the same conclusions as the flat spec.
		"""
		lines = [
			'build/',
			'*.log',
			'!keep.log',
			'build/*',
			'!build/keep.log',
		]
		spec = GitIgnoreSpec.from_lines(lines, backend='simple')
		layers = [DrillDownLayer('', spec)]

		paths = [
			('build/', True),
			('build/keep.log', False),
			('build/drop.log', False),
			('keep.log', False),
			('x.log', False),
		]

		for path, is_dir in paths:
			flat = spec.check_file(path, is_dir=is_dir)
			drilled = check_drilldown(layers, path, is_dir=is_dir)
			self.assertEqual(
				flat.include,
				drilled.include,
				(path, flat, drilled),
			)

	def test_02_negation_inside_excluded_directory(self) -> None:
		"""
		A negation in a deeper .gitignore re-includes the path even though
		the ancestor directory is excluded. The candidate path is descended
		into instead of being pruned early.
		"""
		layers = make_layers({
			'': ['build/'],
			'build': ['!keep.log'],
		})

		result = check_drilldown(layers, 'build/keep.log')
		self.assertIs(result.include, False)
		self.assertIsNotNone(result.layer)
		self.assertEqual(result.layer.dir_path, 'build/')
		self.assertEqual(result.index, 0)

		# A non-negated file remains denied, decided by the root layer.
		result = check_drilldown(layers, 'build/drop.log')
		self.assertIs(result.include, True)
		self.assertEqual(result.layer.dir_path, '')

	def test_03_deepest_layer_wins(self) -> None:
		"""
		When both layers match, the deepest layer decides.
		"""
		layers = make_layers({
			'': ['*.log'],
			'sub': ['!x.log'],
		})

		self.assertIs(check_drilldown(layers, 'sub/x.log').include, False)
		self.assertIs(check_drilldown(layers, 'sub/y.log').include, True)

		# Root pattern decides for files outside the deeper layer.
		self.assertIs(check_drilldown(layers, 'top.log').include, True)

	def test_04_whitelist_idiom_split_across_layers(self) -> None:
		"""
		The whitelist idiom still descends when the keep-rule lives in a
		nested .gitignore. ``!*/`` keeps directories traversable; the nested
		``*.txt`` denies text files, other files remain allowed.
		"""
		layers = make_layers({
			'': ['*', '!*/', '!*.py'],
			'sub': ['*.txt'],
		})

		self.assertIs(check_drilldown(layers, 'sub/a.py').include, False)
		self.assertIs(check_drilldown(layers, 'sub/a.txt').include, True)

	def test_05_no_layer_matches(self) -> None:
		layers = make_layers({'': ['nope']})
		result = check_drilldown(layers, 'other/file')
		self.assertIsNone(result.include)
		self.assertIsNone(result.index)
		self.assertIsNone(result.layer)

	def test_06_plural(self) -> None:
		layers = make_layers({'': ['*.log']})
		results = list(check_drilldown_files(layers, ['a.log', 'a.txt']))
		self.assertEqual([result.include for result in results], [True, None])

	def test_07_directory_own_gitignore_not_applied_to_itself(self) -> None:
		"""
		A directory's own .gitignore does not apply to the directory itself.
		"""
		layers = make_layers({
			'': ['build/', 'x'],
			'build': ['!x'],
		})

		# The build layer is not an ancestor of "build" itself.
		result = check_drilldown(layers, 'build', is_dir=True)
		self.assertIs(result.include, True)

		# But it is an ancestor of files beneath it.
		result = check_drilldown(layers, 'build/x')
		self.assertIs(result.include, False)
