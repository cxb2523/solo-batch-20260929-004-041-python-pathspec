"""
This module provides the resident HTTP service for :class:`.IgnorePolicy`.

Endpoints:

-	``GET /check?path=<path>`` returns the verdict and the matching rule. The
	``is_dir`` query parameter (``"1"``/``"true"`` or ``"0"``/``"false"``) can
	force directory handling. ``format=json`` (or an ``Accept`` header without
	``text/html``) returns JSON; otherwise an HTML page is returned for
	line-by-line review in a browser.
-	``GET /health`` returns a short health report.
"""
from __future__ import annotations

import argparse
import html
import json
import threading
from http.server import (
	BaseHTTPRequestHandler,
	ThreadingHTTPServer)
from typing import (
	Optional)
from urllib.parse import (
	parse_qs,
	urlsplit)

from .policy import (
	Decision,
	IgnorePolicy,
	PolicyError)

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8080


class CheckHTTPServer(ThreadingHTTPServer):
	"""
	The :class:`CheckHTTPServer` class is the threaded HTTP server. Each
	request is handled on its own thread while sharing one thread-safe
	:class:`.IgnorePolicy`.
	"""

	daemon_threads = True
	allow_reuse_address = True

	def __init__(
		self,
		server_address: tuple[str, int],
		policy: IgnorePolicy,
	) -> None:
		self.policy = policy
		super().__init__(server_address, CheckRequestHandler)


class CheckRequestHandler(BaseHTTPRequestHandler):
	"""
	The :class:`CheckRequestHandler` class handles HTTP requests.
	"""

	server_version = 'glsrv/1.0'

	server: CheckHTTPServer

	def do_GET(self) -> None:
		"""
		Handle a ``GET`` request.
		"""
		parsed = urlsplit(self.path)

		if parsed.path == '/health':
			self._write_json(200, {'status': 'ok'})
			return

		if parsed.path in ('/', '/check'):
			query = parse_qs(parsed.query, keep_blank_values=True)
			self._handle_check(parsed.path, query)
			return

		self._write_json(404, {'error': 'not found', 'path': parsed.path})

	def log_message(self, format: str, *args: object) -> None:
		"""
		Log requests concisely to standard error.
		"""
		thread_name = threading.current_thread().name
		super().log_message(f"[{thread_name}] {format}", *args)

	def _handle_check(
		self,
		path: str,
		query: dict[str, list[str]],
	) -> None:
		"""
		Handle ``/`` and ``/check``.
		"""
		query_path = query.get('path', [''])[0]

		if not query_path:
			if self._wants_html(query):
				self._write_html(200, _render_form(self.server.policy.root, None, None))
			else:
				self._write_json(400, {
					'error': 'missing required query parameter: path',
				})
			return

		try:
			is_dir = _parse_bool(query.get('is_dir', [None])[0])
			decision = self.server.policy.check(query_path, is_dir=is_dir)
		except PolicyError as error:
			self._write_json(400, {'error': str(error)})
			return
		except OSError as error:
			self._write_json(500, {'error': str(error)})
			return

		if self._wants_html(query):
			self._write_html(200, _render_result(
				self.server.policy.root, query_path, decision,
			))
		else:
			self._write_json(200, decision.to_dict())

	def _wants_html(self, query: dict[str, list[str]]) -> bool:
		"""
		Determine whether to render HTML. Explicit ``format`` wins; otherwise
		the ``Accept`` header decides, with browsers preferring HTML and
		``curl`` (``*/*`` or JSON-only) receiving JSON.
		"""
		fmt = query.get('format', [None])[0]
		if fmt == 'html':
			return True
		if fmt == 'json':
			return False

		accept = self.headers.get('Accept', '')
		return 'text/html' in accept

	def _write_json(self, status: int, payload: dict[str, object]) -> None:
		"""
		Write a JSON response.
		"""
		body = json.dumps(payload, indent=2, sort_keys=True).encode('utf-8')
		self.send_response(status)
		self.send_header('Content-Type', 'application/json; charset=utf-8')
		self.send_header('Content-Length', str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def _write_html(self, status: int, body: str) -> None:
		"""
		Write an HTML response.
		"""
		data = body.encode('utf-8')
		self.send_response(status)
		self.send_header('Content-Type', 'text/html; charset=utf-8')
		self.send_header('Content-Length', str(len(data)))
		self.end_headers()
		self.wfile.write(data)


def _parse_bool(value: Optional[str]) -> Optional[bool]:
	"""
	Parse a boolean query parameter.

	Returns :data:`None` when *value* is :data:`None`.
	"""
	if value is None:
		return None

	normalized = value.strip().lower()
	if normalized in ('1', 'true', 'yes', 'on'):
		return True
	if normalized in ('0', 'false', 'no', 'off'):
		return False

	raise PolicyError(f"is_dir:{value!r} is not a boolean.")


def create_server(
	root: str,
	host: str = DEFAULT_HOST,
	port: int = DEFAULT_PORT,
) -> CheckHTTPServer:
	"""
	Create a :class:`CheckHTTPServer` for *root* without serving.
	"""
	policy = IgnorePolicy(root)
	return CheckHTTPServer((host, port), policy)


def main(argv: Optional[list[str]] = None) -> int:
	"""
	Run the HTTP service.

	*argv* (:class:`list` of :class:`str` or :data:`None`) contains the
	command-line arguments.

	Returns the process exit code (:class:`int`).
	"""
	parser = argparse.ArgumentParser(
		prog='python -m glsrv',
		description='Serve gitignore checks for a repository over HTTP.',
	)
	parser.add_argument(
		'--root',
		default='.',
		help='repository root directory (default: current directory)',
	)
	parser.add_argument(
		'--host',
		default=DEFAULT_HOST,
		help=f'bind host (default: {DEFAULT_HOST})',
	)
	parser.add_argument(
	'--port',
		type=int,
		default=DEFAULT_PORT,
		help=f'bind port (default: {DEFAULT_PORT})',
	)

	args = parser.parse_args(argv)

	policy = IgnorePolicy(args.root)
	httpd = CheckHTTPServer((args.host, args.port), policy)

	try:
		print(
			f"glsrv serving {policy.root} on "
			f"http://{args.host}:{args.port}/check?path=<path>",
			flush=True,
		)
		httpd.serve_forever()
	except KeyboardInterrupt:
		print("\nshutting down", flush=True)
	finally:
		httpd.server_close()

	return 0


_CSS = """
body { font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
	margin: 2em auto; max-width: 60em; padding: 0 1em; color: #1b1f23; }
form { margin-bottom: 1.5em; }
input[type=text] { font: inherit; padding: .4em .6em; width: 32em;
	border: 1px solid #8c959f; border-radius: 6px; }
button { font: inherit; padding: .4em 1em; border-radius: 6px;
	border: 1px solid #1f6feb; background: #1f6feb; color: white; cursor: pointer; }
.allowed { color: #1a7f37; font-weight: bold; }
.denied { color: #cf222e; font-weight: bold; }
table { border-collapse: collapse; margin-top: .5em; }
td, th { border: 1px solid #d0d7de; padding: .3em .6em; text-align: left; }
.layers { color: #57606a; }
.rule { background: #fff8c5; }
h1 { font-size: 1.2em; }
code { background: #f6f8fa; padding: 0 .25em; border-radius: 4px; }
"""


def _render_form(root: str, path: Optional[str], decision: Optional[Decision]) -> str:
	"""
	Render the query form.
	"""
	return (
		'<!doctype html><html><head><meta charset="utf-8">'
		'<title>glsrv</title>'
		f'<style>{_CSS}</style></head><body>'
		f'<h1>glsrv — gitignore line checker</h1>'
		f'<p>root: <code>{html.escape(root)}</code></p>'
		'<form action="/check" method="get">'
		'<input type="text" name="path" placeholder="path/to/file" '
		f'value="{html.escape(path or "", quote=True)}" autofocus>'
		' <button type="submit">check</button>'
		'</form>'
		f'{_render_decision(decision) if decision is not None else ""}'
		'</body></html>'
	)


def _render_result(root: str, path: str, decision: Decision) -> str:
	"""
	Render the form populated with *path* and its *decision*.
	"""
	return _render_form(root, path, decision)


def _render_decision(decision: Decision) -> str:
	"""
	Render a decision table.
	"""
	verdict = html.escape(decision.verdict)
	kind = 'directory' if decision.is_dir else 'file'

	if decision.rule is None:
		rule_html = '<em>no pattern matched</em>'
	else:
		rule = decision.rule
		rule_html = (
			'<table><tr><th>verdict</th><th>type</th><th>source</th>'
			'<th>line</th><th>pattern</th><th>action</th></tr>'
			f'<tr class="rule"><td class="{verdict}">{verdict}</td>'
			f'<td>{kind}</td>'
			f'<td>{html.escape(rule.source)}</td>'
			f'<td>{rule.line}</td>'
			f'<td><code>{html.escape(rule.pattern)}</code></td>'
			f'<td>{html.escape(rule.action)}</td></tr></table>'
		)

	layers = ', '.join(
		f'{html.escape(layer.source)}{" ✓" if layer.matched else ""}'
		for layer in decision.layers
	) or '<em>none</em>'

	return (
		f'<p><code>{html.escape(decision.path or ".")}</code> is '
		f'<span class="{verdict}">{verdict}</span> ({kind})</p>'
		f'{rule_html}'
		f'<p class="layers">layers considered: {layers}</p>'
	)
