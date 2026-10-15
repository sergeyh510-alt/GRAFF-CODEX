"""Shared static-file helpers used by BOTH web backends.

Why this module exists: on Windows the ``.js`` extension is registered in
``HKEY_CLASSES_ROOT`` as ``text/plain``.  Python's :mod:`mimetypes` reads that
registry value, so ``mimetypes.guess_type("app.js")`` returns ``text/plain`` --
and FastAPI's :class:`StaticFiles` does the same.  Browsers then refuse to
execute the frontend scripts and the page dies with "vis-network did not
load".

To make the behaviour identical on every platform we own one explicit
extension -> content-type table here (never ``mimetypes`` for our own assets)
plus the cache policy.  The FastAPI and stdlib backends both import from this
module so the mapping can never drift apart.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

#: Authoritative extension -> content-type table.  JavaScript *must* be
#: ``text/javascript`` and CSS *must* be ``text/css`` or browsers reject them.
CONTENT_TYPES: Dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}

DEFAULT_CONTENT_TYPE = "application/octet-stream"

#: Assets that must never be served from a stale cache.  A previously cached
#: 404 (or a sniffed text/plain response) used to keep the frontend broken even
#: after the file was fixed, so the page and all of its scripts are no-store.
NO_STORE_EXTS = {".html", ".htm", ".js", ".mjs", ".css", ".json", ".map", ".svg"}

#: Sent on every response so the browser never re-sniffs the type.
NOSNIFF = "nosniff"


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def content_type_for(path: str) -> str:
    """Return the HTTP content-type for ``path`` (never ``text/plain`` for JS)."""

    return CONTENT_TYPES.get(_ext(path), DEFAULT_CONTENT_TYPE)


def cache_control_for(path: str) -> str:
    """Return the Cache-Control value for ``path``."""

    return "no-store" if _ext(path) in NO_STORE_EXTS else "no-cache"


def resolve_static(static_dir: str, relative: str) -> Tuple[Optional[str], int]:
    """Resolve ``relative`` under ``static_dir`` defensively.

    Returns ``(absolute_path, 200)`` for an existing file, otherwise
    ``(None, 403)`` when the path escapes the root or ``(None, 404)`` when the
    file does not exist.
    """

    root = os.path.abspath(static_dir)
    normalised = (relative or "").replace("\\", "/").strip("/")
    target = os.path.abspath(os.path.join(root, *normalised.split("/"))) if normalised else root
    if not (target == root or target.startswith(root + os.sep)):
        return None, 403
    if not os.path.isfile(target):
        return None, 404
    return target, 200


def read_static(static_dir: str, relative: str) -> Tuple[Optional[bytes], str, str, int]:
    """Return ``(body, content_type, cache_control, status)`` for a static file.

    ``body`` is ``None`` on error; ``content_type`` then describes the JSON
    error body so callers can forward it as-is.
    """

    target, status = resolve_static(static_dir, relative)
    if target is None:
        return None, "application/json; charset=utf-8", "no-store", status
    with open(target, "rb") as handle:
        body = handle.read()
    return body, content_type_for(target), cache_control_for(target), 200
