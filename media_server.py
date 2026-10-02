"""Serve AstrBot's downloaded media over HTTP so Max can fetch it.

Max's OneBot edge refuses inline payloads by design: a @data:@ or @base64://@
address is rejected by @mediaRemoteRef@ with the note that such a payload "must
be imported into BlobStore before the body can become canonical".  What Max can
use is an @http(s)@ address, which it enqueues as a bounded fetch.

The QQ adapter downloads inbound images into this container's temp directory
and hands the bridge a path, so the bytes exist -- but a path is meaningless to a
process in another container.  This closes that gap: a tiny read-only file
server over the same directory, which Max reaches by container name.

Deliberately narrow:

* only files under the configured root, after resolving symlinks, so a crafted
  filename cannot walk out of it;
* only media extensions, so this cannot be turned into a general file server;
* GET andHEAD only, nothing that writes;
* a bounded cache-control window, because the files are transient.
"""

from __future__ import annotations

import os
from pathlib import Path

from aiohttp import web

# Extensions worth serving. Anything else is refused rather than guessed at: a
# temp directory may hold session databases and exports alongside images.
MEDIA_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".svg", ".mp4", ".mov", ".webm", ".mp3", ".amr", ".silk", ".wav"}
)


class MediaServer:
    """A read-only HTTP view of one directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port = 0

    async def start(self, host: str, port: int) -> None:
        app = web.Application()
        # add_get covers HEAD too: aiohttp registers it on the same resource,
        # and registering it again raises at startup.
        app.router.add_get("/media/{name}", self._serve)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        self.port = port

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        self._site = None
        self._runner = None

    def resolve(self, name: str) -> Path | None:
        """The readable path for a media name, or None if it is not ours.

        The name arrives from the wire, so containment is checked on the
        resolved path rather than the joined one: joining first and checking
        afterwards still lets a symlink escape.
        """
        if not name or "/" in name or name.startswith("."):
            return None
        if Path(name).suffix.lower() not in MEDIA_SUFFIXES:
            return None
        candidate = (self.root / name).resolve()
        if candidate.parent != self.root:
            return None
        return candidate if candidate.is_file() else None

    async def _serve(self, request: web.Request) -> web.StreamResponse:
        path = self.resolve(request.match_info.get("name", ""))
        if path is None:
            raise web.HTTPNotFound(text="no such media")
        # The file is a transient download; letting Max cache it would outlive
        # the file itself.  FileResponse infers the content type from the
        # suffix and rejects an explicit one.
        return web.FileResponse(path, headers={"Cache-Control": "private, max-age=300"})


def media_url(host: str, port: int, name: str) -> str:
    """The address Max will fetch, by container name rather than localhost."""
    return f"http://{host}:{port}/media/{name}"
