"""
This script tests the resident ``glsrv`` HTTP service and its policy engine.
"""
import json
import os
import os.path
import pathlib
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
from concurrent.futures import (
	ThreadPoolExecutor)
from urllib.error import (
	HTTPError)

from glsrv.policy import (
	IgnorePolicy,
	PolicyError)
from glsrv.server import (
	create_server)


class PolicyRepoTest(unittest.TestCase):
	"""
	The :class:`PolicyRepoTest` class creates temporary repositories.
	"""

	def setUp(self) -> None:
		self.temp_dir = pathlib.Path(tempfile.mkdtemp(prefix='glsrv_test_'))

	def tearDown(self) -> None:
		shutil.rmtree(self.temp_dir, ignore_errors=True)

	def write(self, rel_path: str, text: str) -> None:
		"""
		Write *text* to *rel_path*, creating parent directories.
		"""
		full = self.temp_dir / rel_path
		full.parent.mkdir(parents=True, exist_ok=True)
		full.write_text(text, encoding='utf-8', newline='\n')

	def touch(self, rel_path: str, is_dir: bool = False) -> None:
		"""
		Create an empty file or directory.
		"""
		full = self.temp_dir / rel_path
		full.parent.mkdir(parents=True, exist_ok=True)
		if is_dir:
			full.mkdir(exist_ok=True)
		else:
			full.touch()


class PolicyEngineTest(PolicyRepoTest):
	"""
	The :class:`PolicyEngineTest` class tests :class:`.IgnorePolicy`.
	"""

	def test_01_basic_verdict_and_rule(self) -> None:
		self.write('.gitignore', '*.log\n')

		policy = IgnorePolicy(self.temp_dir)

		denied = policy.check('a.log')
		self.assertEqual(denied.verdict, 'denied')
		self.assertTrue(denied.ignored)
		self.assertIsNotNone(denied.rule)
		assert denied.rule is not None
		self.assertEqual(denied.rule.source, '.gitignore')
		self.assertEqual(denied.rule.line, 1)
		self.assertEqual(denied.rule.pattern, '*.log')
		self.assertEqual(denied.rule.action, 'ignore')

		allowed = policy.check('a.txt')
		self.assertEqual(allowed.verdict, 'allowed')
		self.assertIsNone(allowed.ignored)
		self.assertIsNone(allowed.rule)

	def test_02_no_early_pruning_negation_in_excluded_dir(self) -> None:
		"""
		The engine descends into an excluded directory and honors a negation
		in the nested .gitignore.
		"""
		self.write('.gitignore', 'build/\n')
		self.write('build/.gitignore', '# comment\n\n!keep.log\n')
		self.touch('build/keep.log')
		self.touch('build/drop.log')

		policy = IgnorePolicy(self.temp_dir)

		keep = policy.check('build/keep.log')
		self.assertEqual(keep.verdict, 'allowed')
		self.assertFalse(keep.ignored)
		assert keep.rule is not None
		self.assertEqual(keep.rule.source, 'build/.gitignore')
		self.assertEqual(keep.rule.line, 3)
		self.assertEqual(keep.rule.pattern, '!keep.log')
		self.assertEqual(keep.rule.action, 'unignore')

		drop = policy.check('build/drop.log')
		self.assertEqual(drop.verdict, 'denied')
		assert drop.rule is not None
		self.assertEqual(drop.rule.source, '.gitignore')

	def test_03_is_dir_cache_keys_are_separate(self) -> None:
		"""
		A file and a directory with the same name use distinct cache entries
		and cannot pollute each other.
		"""
		self.write('.gitignore', 'name/\n')
		self.touch('name', is_dir=True)

		policy = IgnorePolicy(self.temp_dir)

		directory = policy.check('name/')
		file_like = policy.check('name', is_dir=False)

		self.assertTrue(directory.is_dir)
		self.assertFalse(file_like.is_dir)
		self.assertTrue(directory.ignored)
		self.assertIsNone(file_like.ignored)

		# Both keys exist independently in the cache.
		self.assertEqual(policy.stats['decisions'], 2)

	def test_04_restart_consistency(self) -> None:
		"""
		Results from a fresh engine (simulated restart) match the warmed
		engine; the cache only accelerates.
		"""
		self.write('.gitignore', 'build/\n*.log\n')
		self.write('build/.gitignore', '!keep.log\n')

		paths = [
			'build/keep.log',
			'build/drop.log',
			'top.log',
			'top.txt',
			'build',
		]

		first = IgnorePolicy(self.temp_dir)
		warmed = {path: first.check(path).to_dict() for path in paths}
		# Repeat requests exercise the decision cache.
		for path in paths:
			first.check(path)
		self.assertGreater(first.stats['hits'], 0)

		# Restart: new engine, empty in-memory cache.
		second = IgnorePolicy(self.temp_dir)
		restarted = {path: second.check(path).to_dict() for path in paths}

		for path in paths:
			self.assertEqual(warmed[path], restarted[path], path)

		# Invalidating and re-evaluating also yields the same conclusions.
		first.invalidate()
		for path in paths:
			self.assertEqual(
				first.check(path).to_dict(),
				warmed[path],
				path,
			)

	def test_05_edit_changes_verdict(self) -> None:
		"""
		Editing a .gitignore invalidates the cache and the new verdict is
		visible.
		"""
		self.write('.gitignore', 'a.log\n')

		policy = IgnorePolicy(self.temp_dir)
		self.assertEqual(policy.check('a.log').verdict, 'denied')

		self.write('.gitignore', 'b.log\n')
		target = self.temp_dir / '.gitignore'
		timestamp = time.time() + 5
		os.utime(target, (timestamp, timestamp))

		self.assertEqual(policy.check('a.log').verdict, 'allowed')
		self.assertEqual(policy.check('b.log').verdict, 'denied')

	def test_06_path_escape_is_rejected(self) -> None:
		policy = IgnorePolicy(self.temp_dir)

		with self.assertRaises(PolicyError):
			policy.check('../../etc/passwd')

		with self.assertRaises(PolicyError):
			policy.check(os.path.abspath(os.path.join(self.temp_dir, '..')))

	def test_07_concurrent_readers_see_complete_snapshots(self) -> None:
		"""
		Concurrent requests must only observe complete, internally consistent
		decisions while a .gitignore file is being written.
		"""
		self.write('.gitignore', '*.log\n')
		(self.temp_dir / 'a').mkdir(parents=True, exist_ok=True)
		(self.temp_dir / 'a' / '.gitignore').write_text(
			'!x.log\n', encoding='utf-8',
		)

		policy = IgnorePolicy(self.temp_dir)

		paths = [f'a/{name}.log' for name in range(4)] + [
			f'{name}.log' for name in range(4)
		]

		stop = threading.Event()
		errors: list[BaseException] = []

		def writer() -> None:
			try:
				target = self.temp_dir / 'a' / '.gitignore'
				variants = ['!x.log\n', '*.log\n', '!*.log\n', 'x.log\n']
				index = 0
				while not stop.is_set():
					target.write_text(variants[index % len(variants)], encoding='utf-8')
					timestamp = time.time() + index
					os.utime(target, (timestamp, timestamp))
					index += 1
					time.sleep(0.002)
			except BaseException as error:  # pragma: no cover - test reporting
				errors.append(error)

		def reader() -> None:
			try:
				for _ in range(20):
					for path in paths:
						decision = policy.check(path)
						self._assert_complete_decision(decision)
			except BaseException as error:  # pragma: no cover - test reporting
				errors.append(error)

		writer_thread = threading.Thread(target=writer)
		writer_thread.start()

		with ThreadPoolExecutor(max_workers=8) as pool:
			futures = [pool.submit(reader) for _ in range(4)]
			for future in futures:
				future.result()

		stop.set()
		writer_thread.join()

		self.assertEqual(errors, [])

	@staticmethod
	def _assert_complete_decision(decision: object) -> None:
		"""
		Assert a decision is a complete immutable snapshot.
		"""
		assert decision is not None
		assert hasattr(decision, 'verdict')
		assert decision.verdict in ('allowed', 'denied')
		if decision.rule is not None:
			assert decision.rule.line >= 1
			assert decision.rule.source.endswith('.gitignore')
			assert decision.rule.action in ('ignore', 'unignore')
			matched = [layer for layer in decision.layers if layer.matched]
			assert len(matched) == 1
			assert matched[0].source == decision.rule.source

	def test_08_large_tree_drilldown_is_fast(self) -> None:
		"""
		Drill-down over a large tree stays fast thanks to cached compiled
		layers.
		"""
		depth = 60
		for index in range(depth):
			rel = '/'.join(['d'] * index)
			directory = self.temp_dir / rel if rel else self.temp_dir
			directory.mkdir(parents=True, exist_ok=True)
			(directory / '.gitignore').write_text('*.tmp\n', encoding='utf-8')

		policy = IgnorePolicy(self.temp_dir)

		target = '/'.join(['d'] * (depth - 1) + ['file.txt'])

		start = time.perf_counter()
		for _ in range(100):
			decision = policy.check(target, refresh=True)
		elapsed = time.perf_counter() - start

		self.assertEqual(decision.verdict, 'allowed')
		self.assertLess(elapsed, 2.0, elapsed)


class HttpServiceTest(PolicyRepoTest):
	"""
	The :class:`HttpServiceTest` class tests the running HTTP service.
	"""

	def setUp(self) -> None:
		super().setUp()
		self.write('.gitignore', 'build/\n*.log\n')
		self.write('build/.gitignore', '!keep.log\n')
		self.server = create_server(str(self.temp_dir), '127.0.0.1', 0)
		self.port = self.server.server_address[1]
		self.thread = threading.Thread(
			target=self.server.serve_forever, daemon=True,
		)
		self.thread.start()

	def tearDown(self) -> None:
		self.server.shutdown()
		self.server.server_close()
		self.thread.join(timeout=5)
		super().tearDown()

	def _get(
		self,
		query: str,
		accept: str = '*/*',
	) -> tuple[int, object]:
		url = f'http://127.0.0.1:{self.port}{query}'
		request = urllib.request.Request(url, headers={'Accept': accept})
		try:
			with urllib.request.urlopen(request, timeout=10) as response:
				body = response.read().decode('utf-8')
				status = response.status
		except HTTPError as error:
			body = error.read().decode('utf-8')
			status = error.code

		if 'text/html' in accept and status == 200:
			return status, body
		return status, json.loads(body)

	def test_01_health(self) -> None:
		status, payload = self._get('/health')
		self.assertEqual(status, 200)
		self.assertEqual(payload, {'status': 'ok'})

	def test_02_check_json_allowed(self) -> None:
		status, payload = self._get('/check?path=build/keep.log')
		self.assertEqual(status, 200)
		assert isinstance(payload, dict)
		self.assertEqual(payload['verdict'], 'allowed')
		self.assertFalse(payload['ignored'])
		self.assertEqual(payload['rule']['line'], 1)
		self.assertEqual(payload['rule']['source'], 'build/.gitignore')

	def test_03_check_denied(self) -> None:
		status, payload = self._get('/check?path=top.log')
		self.assertEqual(status, 200)
		assert isinstance(payload, dict)
		self.assertEqual(payload['verdict'], 'denied')
		self.assertTrue(payload['ignored'])

	def test_04_missing_path_is_400(self) -> None:
		status, payload = self._get('/check')
		self.assertEqual(status, 400)
		assert isinstance(payload, dict)
		self.assertIn('path', payload['error'])

	def test_05_escape_is_400(self) -> None:
		status, _payload = self._get('/check?path=../../Windows/win.ini')
		self.assertEqual(status, 400)

	def test_06_is_dir_query(self) -> None:
		status, payload = self._get('/check?path=build&is_dir=1')
		self.assertEqual(status, 200)
		assert isinstance(payload, dict)
		self.assertTrue(payload['is_dir'])

		status, payload = self._get('/check?path=build&is_dir=0')
		assert isinstance(payload, dict)
		self.assertFalse(payload['is_dir'])

	def test_07_html_page(self) -> None:
		status, body = self._get('/check?path=top.log', accept='text/html')
		self.assertEqual(status, 200)
		assert isinstance(body, str)
		self.assertIn('<html', body)
		self.assertIn('denied', body)
		self.assertIn('.gitignore', body)

	def test_08_unknown_route_is_404(self) -> None:
		status, _payload = self._get('/unknown')
		self.assertEqual(status, 404)

	def test_09_concurrent_http_requests(self) -> None:
		"""
		The threaded service serves many concurrent requests consistently.
		"""
		paths = [
			'build/keep.log',
			'top.log',
			'top.txt',
			'build/x.log',
		]

		def request(path: str) -> tuple[str, str]:
			status, payload = self._get(f'/check?path={path}')
			assert status == 200
			assert isinstance(payload, dict)
			return (path, payload['verdict'])

		with ThreadPoolExecutor(max_workers=8) as pool:
			results = list(pool.map(
				lambda _: [request(path) for path in paths],
				range(20),
			))

		expected = {
			'build/keep.log': 'allowed',
			'top.log': 'denied',
			'top.txt': 'allowed',
			'build/x.log': 'denied',
		}

		for batch in results:
			self.assertEqual(dict(batch), expected)
