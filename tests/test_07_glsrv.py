"""
Tests for the :mod:`glsrv` persistent gitignore decision service.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request

import pytest

from glsrv.engine import (
	Decision,
	GitIgnoreEngine,
	_DirCache)
from glsrv.server import (
	make_server)
from pathspec.gitignore import (
	GitIgnoreSpec)
from pathspec.util import (
	normalize_file)

from tests.util import (
	make_dirs,
	make_files)

GITIGNORE = "\n".join([
	"# glsrv test fixture",
	"",
	"*.log",
	"!keep.log",
	"build/",
	"cache/*",
	"!cache/keep.txt",
	"**/temp*",
	"docs/**/*.md",
	"onlydir/",
])

DIRS = [
	'build',
	'cache',
	'docs',
	'docs/a',
	'onlydir',
	'x',
]

FILES = [
	'a.log',
	'keep.log',
	'build/x.o',
	'cache/keep.txt',
	'cache/junk.bin',
	'docs/a/b.md',
	'docs/a/b.txt',
	'x/temp1',
	'onlydir/f.txt',
]


@pytest.fixture
def repo(tmp_path):
	"""
	Creates a fixture repository with a ``.gitignore`` and returns its path.
	"""
	(tmp_path / '.gitignore').write_text(GITIGNORE, encoding='utf-8')
	make_dirs(tmp_path, DIRS)
	make_files(tmp_path, FILES)
	return tmp_path


@pytest.fixture
def engine(repo):
	"""
	Returns a :class:`GitIgnoreEngine` for the fixture repository.
	"""
	return GitIgnoreEngine(repo)


def _reference_spec():
	"""
	Builds the in-process spec from the same lines as the fixture.
	"""
	return GitIgnoreSpec.from_lines(GITIGNORE.splitlines())


def test_check_decisions(engine):
	"""
	Basic allowed/denied decisions, including re-inclusion by negation.
	"""
	assert engine.check('a.log').verdict == 'denied'
	assert engine.check('keep.log').verdict == 'allowed'
	assert engine.check('build/x.o').verdict == 'denied'
	# Descend into "cache/" (excluded by "cache/*") and let the negation rule
	# re-include the file instead of pruning the directory early.
	assert engine.check('cache/keep.txt').verdict == 'allowed'
	assert engine.check('cache/junk.bin').verdict == 'denied'
	assert engine.check('x/temp1').verdict == 'denied'
	assert engine.check('docs/a/b.md').verdict == 'denied'
	assert engine.check('docs/a/b.txt').verdict == 'allowed'


def test_check_reports_rule_and_line(engine):
	"""
	The matched rule carries the .gitignore line number and pattern text.
	"""
	decision = engine.check('cache/keep.txt')
	assert decision.hit is not None
	assert decision.hit.line == 7
	assert decision.hit.pattern == '!cache/keep.txt'
	assert decision.hit.include is False

	decision = engine.check('x/temp1')
	assert decision.hit is not None
	assert decision.hit.line == 8
	assert decision.hit.pattern == '**/temp*'

	decision = engine.check('docs/a/b.txt')
	assert decision.hit is None


def test_trailing_slash_and_dir_detection(engine):
	"""
	Trailing slashes mark directories; the file-system is consulted otherwise.
	"""
	assert normalize_file('onlydir/') == 'onlydir/'

	norm, is_dir = engine.normalize('onlydir/')
	assert (norm, is_dir) == ('onlydir/', True)

	# Detected from the file-system.
	norm, is_dir = engine.normalize('onlydir')
	assert (norm, is_dir) == ('onlydir/', True)

	norm, is_dir = engine.normalize('a.log')
	assert (norm, is_dir) == ('a.log', False)

	# A directory-only pattern denies the directory but not a same-named file.
	assert engine.check('onlydir', is_dir=True).verdict == 'denied'
	assert engine.check('onlydir', is_dir=False).verdict == 'allowed'


def test_cache_key_includes_is_dir(engine):
	"""
	Same-named files and directories get separate cache entries.
	"""
	file_decision = engine.check('onlydir', is_dir=False)
	dir_decision = engine.check('onlydir', is_dir=True)
	assert file_decision.verdict == 'allowed'
	assert dir_decision.verdict == 'denied'

	snapshot = engine._cache.snapshot('onlydir')
	assert snapshot[('onlydir', False)] == file_decision
	assert snapshot[('onlydir/', True)] == dir_decision


def test_cache_snapshot_is_copy_on_write():
	"""
	Writers swap in complete bucket snapshots; existing snapshots are never
	mutated.
	"""
	cache = _DirCache()
	first = Decision(path='a/b', is_dir=False, denied=True, hit=None)
	cache.put('a/b', False, first)

	bucket_before = cache._buckets['a']
	second = Decision(path='a/c', is_dir=False, denied=False, hit=None)
	cache.put('a/c', False, second)

	# The old snapshot is untouched by the write.
	assert bucket_before == {('a/b', False): first}
	assert cache.get('a/b', False) == first
	assert cache.get('a/c', False) == second


def test_cache_concurrent_readers_see_complete_snapshots(engine):
	"""
	Concurrent writers and readers on the same directory cache never observe
	half-updated state.
	"""
	paths = [f'cache/file{i}.txt' for i in range(50)]
	errors = []

	def worker():
		try:
			for _ in range(10):
				for path in paths:
					decision = engine.check(path)
					assert isinstance(decision, Decision)
					assert decision.path == path
					assert decision.is_dir is False
		except AssertionError as e:
			errors.append(e)

	threads = [threading.Thread(target=worker) for _ in range(8)]
	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join()

	assert not errors
	snapshot = engine._cache.snapshot('cache/file0.txt')
	for path in paths:
		assert (path, False) in snapshot


def test_restart_consistency(engine, repo):
	"""
	A fresh engine (as after a service restart) reaches identical conclusions;
	the cache only accelerates.
	"""
	paths = FILES + [__d + '/' for __d in DIRS]
	before = {__p: engine.check(__p) for __p in paths}

	restarted = GitIgnoreEngine(repo)
	for path in paths:
		assert restarted.check(path) == before[path]

	engine.clear_cache()
	for path in paths:
		assert engine.check(path) == before[path]


def test_consistency_with_library_entries(engine):
	"""
	The service, ``GitIgnoreSpec.check_file``/``check_files``, and
	``PathSpec.match_file`` reach the same conclusion for the same path.
	"""
	spec = _reference_spec()
	paths = FILES + [__d + '/' for __d in DIRS] + ['onlydir', 'build']

	for path in paths:
		norm, _is_dir = engine.normalize(path)
		expected = spec.match_file(norm)
		assert engine.check(path).denied == expected, path
		assert bool(spec.check_file(norm).include) == expected, path

	norm_paths = [engine.normalize(__path)[0] for __path in paths]
	results = {__res.file: __res for __res in spec.check_files(norm_paths)}
	for path, norm in zip(paths, norm_paths):
		assert bool(results[norm].include) == spec.match_file(norm), path


def test_explain_descends_without_pruning(engine):
	"""
	The ancestor chain is fully evaluated even when a directory is excluded.
	"""
	chain, final = engine.explain('cache/keep.txt')
	assert [__step.path for __step in chain] == ['cache/', 'cache/keep.txt']
	# "cache/" itself is not matched, but its contents are excluded by
	# "cache/*"; the final negation still re-includes the file.
	assert final.verdict == 'allowed'
	assert final.hit is not None and final.hit.pattern == '!cache/keep.txt'

	chain, final = engine.explain('build/x.o')
	assert chain[0].path == 'build/'
	assert chain[0].verdict == 'denied'
	assert final.verdict == 'denied'


def test_compat_star_star_and_no_negation(engine):
	"""
	``**`` patterns and negation-free rule sets keep their old conclusions.
	"""
	assert engine.check('x/temp1').denied is True
	assert engine.check('deep/nested/temp9', is_dir=False).denied is True
	assert engine.check('docs/a/b.md').denied is True
	assert engine.check('docs/a/b.txt').denied is False


def test_compat_no_negation_rules(repo):
	"""
	A gitignore without negation behaves exactly as before.
	"""
	(repo / '.gitignore').write_text("*.log\ntmp/\n", encoding='utf-8')
	make_dirs(repo, ['tmp'])
	no_neg = GitIgnoreEngine(repo)
	assert no_neg.check('a.log').verdict == 'denied'
	assert no_neg.check('keep.log').verdict == 'denied'
	assert no_neg.check('tmp/').verdict == 'denied'
	assert no_neg.check('tmp/file.txt').verdict == 'denied'
	assert no_neg.check('docs/a/b.txt').verdict == 'allowed'


def test_http_service(engine):
	"""
	The running service answers /check, /health, and / over HTTP.
	"""
	server = make_server(engine, '127.0.0.1', 0)
	port = server.socket.getsockname()[1]
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	try:
		with urllib.request.urlopen(f'http://127.0.0.1:{port}/check?path=cache/keep.txt') as resp:
			payload = json.load(resp)
		assert payload['decision'] == 'allowed'
		assert payload['is_dir'] is False
		assert payload['matched']['line'] == 7
		assert payload['matched']['pattern'] == '!cache/keep.txt'
		assert [__step['path'] for __step in payload['chain']] == ['cache/', 'cache/keep.txt']

		with urllib.request.urlopen(f'http://127.0.0.1:{port}/check?path=build/x.o') as resp:
			payload = json.load(resp)
		assert payload['decision'] == 'denied'
		assert payload['matched']['pattern'] == 'build/'

		with urllib.request.urlopen(f'http://127.0.0.1:{port}/check?path=onlydir&is_dir=1') as resp:
			payload = json.load(resp)
		assert payload['decision'] == 'denied'
		assert payload['is_dir'] is True

		with urllib.request.urlopen(f'http://127.0.0.1:{port}/health') as resp:
			payload = json.load(resp)
		assert payload['status'] == 'ok'

		with urllib.request.urlopen(f'http://127.0.0.1:{port}/') as resp:
			page = resp.read().decode('utf-8')
		assert '!cache/keep.txt' in page

		with urllib.request.urlopen(f'http://127.0.0.1:{port}/check?path=a.log&fmt=html') as resp:
			page = resp.read().decode('utf-8')
		assert 'denied' in page
	finally:
		server.shutdown()
		server.server_close()
		thread.join()
