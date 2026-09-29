# glsrv — resident gitignore checker

A small, dependency-free HTTP service that checks paths against the
`.gitignore` files in a repository.

## Start

```sh
python -m glsrv --root <repo>
```

Optional flags: `--host 127.0.0.1` (default) and `--port 8080` (default).

## Check a path

```sh
curl "http://127.0.0.1:8080/check?path=build/keep.log"
```

Open the same URL in a browser for an HTML view suitable for line-by-line
review and screen recording. `format=json` or `format=html` forces the response
type.

Response:

```json
{
  "path": "build/keep.log",
  "is_dir": false,
  "verdict": "allowed",
  "ignored": false,
  "rule": {
    "source": "build/.gitignore",
    "line": 1,
    "pattern": "!keep.log",
    "action": "unignore"
  },
  "layers": [
    {"source": ".gitignore", "matched": false},
    {"source": "build/.gitignore", "matched": true}
  ]
}
```

- `verdict` is `allowed` or `denied` (ignored).
- `is_dir` is taken from `is_dir=1/0`, a trailing slash, or the file system.
- `rule` is the deciding pattern with its `.gitignore` file and line number.
- `layers` lists every `.gitignore` file considered, root to deepest.

`GET /health` returns `{"status": "ok"}`.

## Semantics

- **Drill-down, no early pruning.** Excluded ancestor directories are still
  descended into, so a negation in a deeper `.gitignore` re-includes the path.
  The deepest layer with a match decides.
- **Files and directories are separate.** Cache keys include `is_dir`, so a
  file and directory with the same name never pollute each other.
- **Atomic snapshots.** Directory and decision caches hold immutable objects
  published with single assignments under a lock; readers never observe a
  half-updated state.
- **Cache only accelerates.** Every entry is validated against the
  `mtime`/size/inode/device of its `.gitignore` files. A restart recomputes
  identical results.
