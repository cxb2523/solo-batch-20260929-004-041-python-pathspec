"""
This module provides the persistent HTTP service for gitignore re-inclusion
decisions. Run it with ``python -m glsrv --root <repo>`` and query
``GET /check?path=...``.
"""
from __future__ import annotations

import html
import json
from http.server import (
	BaseHTTPRequestHandler,
	ThreadingHTTPServer)
from typing import (
	Optional,  # Replaced by `X | None` in 3.10.
	Union)  # Replaced by `X | Y` in 3.10.
from urllib.parse import (
	parse_qs,
	urlparse)

from glsrv.engine import (
	Decision,
	GitIgnoreEngine)
from pathspec.util import (
	StrPath)


def _decision_to_dict(decision: Decision) -> dict:
	"""
	Converts a :class:`.Decision` to a JSON-ready :class:`dict`.
	"""
	hit = decision.hit
	return {
		'path': decision.path,
		'is_dir': decision.is_dir,
		'decision': decision.verdict,
		'matched': {
			'index': hit.index,
			'line': hit.line,
			'pattern': hit.pattern,
			'include': hit.include,
		} if hit is not None else None,
	}


def _render_check_html(query: str, chain: list[Decision], final: Decision) -> str:
	"""
	Renders the check result chain as an HTML page for line-by-line review in
	a browser.
	"""
	rows = []
	for step in chain:
		hit = step.hit
		rule = f"{hit.line}: {html.escape(hit.pattern)}" if hit is not None else '-'
		rows.append(
			f"<tr class='{step.verdict}'><td>{html.escape(step.path)}</td>"
			f"<td>{'dir' if step.is_dir else 'file'}</td>"
			f"<td>{step.verdict}</td><td>{rule}</td></tr>"
		)

	return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>glsrv check</title>
<style>
body {{ font-family: sans-serif; margin: 2em; }}
table {{ border-collapse: collapse; }}
td, th {{ border: 1px solid #ccc; padding: 4px 10px; }}
.denied {{ background: #ffe0e0; }}
.allowed {{ background: #e0ffe0; }}
</style></head><body>
<h1>glsrv check</h1>
<p>path: <code>{html.escape(query)}</code> &rarr; <strong>{final.verdict}</strong></p>
<table><tr><th>path</th><th>kind</th><th>decision</th><th>rule (line: pattern)</th></tr>
{''.join(rows)}
</table>
<p><a href="/">&larr; rules</a></p>
</body></html>"""


def _render_index_html(engine: GitIgnoreEngine) -> str:
	"""
	Renders the index page listing every gitignore rule with its line number.
	"""
	rows = ''.join(
		f"<tr><td>{lineno}</td><td><code>{html.escape(text)}</code></td></tr>"
		for lineno, text in engine.rules
	)
	return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>glsrv</title>
<style>
body {{ font-family: sans-serif; margin: 2em; }}
td, th {{ border: 1px solid #ccc; padding: 4px 10px; }}
table {{ border-collapse: collapse; }}
</style></head><body>
<h1>glsrv &mdash; gitignore decisions</h1>
<form action="/check" method="get">
<label>path: <input name="path" size="50"></label>
<label><input type="checkbox" name="is_dir" value="1"> is directory</label>
<input type="hidden" name="fmt" value="html">
<button type="submit">check</button>
</form>
<h2>rules (.gitignore)</h2>
<table><tr><th>line</th><th>pattern</th></tr>
{rows}
</table>
</body></html>"""


def _parse_is_dir(value: Optional[str]) -> Optional[bool]:
	"""
	Parses the ``is_dir`` query parameter.
	"""
	if value is None:
		return None

	return value.lower() in ('1', 'true', 'yes', 'on')


def make_server(
	engine: GitIgnoreEngine,
	host: str = '127.0.0.1',
	port: int = 8000,
) -> ThreadingHTTPServer:
	"""
	Creates the HTTP server for *engine*. Use port ``0`` to pick an ephemeral
	port.
	"""

	class CheckHandler(BaseHTTPRequestHandler):

		def _send(self, status: int, body: str, content_type: str) -> None:
			data = body.encode('utf-8')
			self.send_response(status)
			self.send_header('Content-Type', content_type)
			self.send_header('Content-Length', str(len(data)))
			self.end_headers()
			self.wfile.write(data)

		def _send_json(self, status: int, payload: dict) -> None:
			self._send(status, json.dumps(payload, indent=2), 'application/json; charset=utf-8')

		def do_GET(self) -> None:  # noqa: N802
			parsed = urlparse(self.path)
			params = parse_qs(parsed.query)

			if parsed.path == '/health':
				self._send_json(200, {'status': 'ok', 'root': engine.root})

			elif parsed.path == '/check':
				query = params.get('path', [''])[0]
				is_dir = _parse_is_dir(params.get('is_dir', [None])[0])
				chain, final = engine.explain(query, is_dir)
				payload = {
					'path': query,
					'normalized': final.path,
					'is_dir': final.is_dir,
					'decision': final.verdict,
					'matched': _decision_to_dict(final)['matched'],
					'chain': [_decision_to_dict(__step) for __step in chain],
				}
				if params.get('fmt', ['json'])[0] == 'html':
					self._send(200, _render_check_html(query, chain, final), 'text/html; charset=utf-8')
				else:
					self._send_json(200, payload)

			elif parsed.path == '/':
				self._send(200, _render_index_html(engine), 'text/html; charset=utf-8')

			else:
				self._send_json(404, {'error': f"unknown path: {parsed.path}"})

		def log_message(self, format: str, *args: object) -> None:  # noqa: A002
			# Keep the service quiet; decisions are observable via /check.
			pass

	return ThreadingHTTPServer((host, port), CheckHandler)


def serve(
	root: StrPath,
	host: str = '127.0.0.1',
	port: int = 8000,
) -> None:
	"""
	Runs the persistent gitignore decision service for the repository at
	*root*.
	"""
	engine = GitIgnoreEngine(root)
	server = make_server(engine, host, port)
	sock = server.socket.getsockname()
	print(f"glsrv: serving {engine.root} on http://{sock[0]}:{sock[1]}")
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		pass
	finally:
		server.server_close()
