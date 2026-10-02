"""An OpenAI-compatible endpoint that borrows AstrBot's model credentials.

Max needs a URL and a key.  The key lives in AstrBot's provider config, and
copying it into Max's environment would put the same secret in two places, two
files and two log surfaces -- and it would still be the wrong key after the
next provider change in the WebUI.

So the bridge does not copy anything.  It exposes a small pass-through on
``POST /v1/chat/completions`` and forwards each request, body untouched, to
whatever provider AstrBot currently has selected, using that provider's own
key and base URL.  Three consequences worth stating:

* The secret never leaves AstrBot.  Max sends a placeholder and the bridge
  swaps in the real credential per request.
* Changing or adding a provider in the WebUI changes Max's model on the next
  request, with no restart on either side.
* Nothing is interpreted.  Tools, streaming and response shapes are Max's and
  the vendor's, so this cannot become a place where a payload is quietly
  rewritten.

The failure mode is deliberate too: with no usable provider, the endpoint
answers 503 with a reason naming the provider, because silently succeeding
would send Max an empty conversation instead of an error.
"""

from __future__ import annotations

import json
from typing import Any

import aiohttp
from aiohttp import web

from astrbot.api.all import logger

# Where Max sends its request: {MAX_LLM_BASE_URL}/chat/completions
PROXY_PREFIX = "/v1"


def _summarise(body: dict[str, Any]) -> str:
    """A shape summary of a forwarded request, not the whole thing.

    The system prompt is thousands of characters of persona and the tool
    descriptions are thousands more, so a full dump says little and drowns the
    log. What matters when a model misbehaves is the shape: which fields are
    present, how many tools, their names, and whether anything in the tool
    definitions is malformed.
    """
    tools = body.get("tools") or []
    lines = [
        f"model={body.get('model')!r}",
        f"stream={body.get('stream')!r}",
        f"messages={len(body.get('messages') or [])}",
        f"tool_choice={body.get('tool_choice')!r}",
        f"parallel_tool_calls={body.get('parallel_tool_calls')!r}",
        f"tools={len(tools)}",
    ]
    for tool in tools[:6]:
        function = (tool or {}).get("function") or {}
        lines.append(
            "  tool "
            f"name={function.get('name')!r} "
            f"desc_len={len(str(function.get('description') or ''))} "
            f"params_keys={sorted((function.get('parameters') or {}).keys())}"
        )
    if len(tools) > 6:
        names = [((t or {}).get('function') or {}).get('name') for t in tools[6:]]
        lines.append(f"  ...and {len(names)} more: {names}")
    # A message carrying tool calls the provider never defined is the shape
    # that produces exactly the failure being chased here.
    for message in body.get("messages") or []:
        if message.get("tool_calls"):
            names = [(c or {}).get("function", {}).get("name") for c in message["tool_calls"]]
            lines.append(f"  history tool_calls={names}")
    return "\n".join(lines)


def _debug_body() -> bool:
    """Dump each forwarded request. Off unless MAX_BRIDGE_LLM_DEBUG=1."""
    import os

    return os.environ.get("MAX_BRIDGE_LLM_DEBUG", "") in {"1", "true", "yes"}


def model_name_of(provider: Any) -> str | None:
    """The provider's own model id.

    AstrBot stores it on @model_name@ (set through @set_model@) while
    @ProviderMeta.model@ is the dataclass field the WebUI listing reads, so
    both spellings are tried before giving up.  Sending Max's model name to a
    vendor that never heard of it comes back as "model is not found", which
    reads like a network fault rather than a name mismatch.
    """
    for attribute in ("model_name", "model"):
        value = getattr(provider, attribute, None)
        if value:
            return str(value)
    return None


class LlmProxy:
    """Serves Max's model traffic through AstrBot's configured provider."""

    def __init__(self, provider_manager: Any, session: aiohttp.ClientSession) -> None:
        self._providers = provider_manager
        self._session = session
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port = 0

    # -- lifecycle --------------------------------------------------------

    async def start(self, host: str, port: int) -> None:
        app = web.Application()
        app.router.add_post(f"{PROXY_PREFIX}/chat/completions", self.chat_completions)
        # Max's transport probes the model list on some paths; answering with
        # the provider that is actually selected keeps that probe truthful.
        app.router.add_get(f"{PROXY_PREFIX}/models", self.models)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        self.port = port
        logger.info(f"[max_bridge] llm proxy on {host}:{port}{PROXY_PREFIX}")

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        self._site = None
        self._runner = None

    # -- provider lookup ---------------------------------------------------

    def _current(self) -> Any | None:
        """The provider AstrBot would use right now.

        Asked for on every request rather than cached: the point of borrowing
        the configuration is that a change in the WebUI takes effect without
        anything here being restarted.
        """
        try:
            from astrbot.core.provider.entities import ProviderType

            chosen = self._providers.get_using_provider(ProviderType.CHAT_COMPLETION)
        except Exception as exc:  # a missing provider must not kill the plugin
            logger.warning(f"[max_bridge] could not read the current provider: {exc!r}")
            return None
        if chosen is not None:
            return chosen
        instances = getattr(self._providers, "provider_insts", None) or []
        return instances[0] if instances else None

    @staticmethod
    def _credential(provider: Any) -> tuple[str | None, str | None]:
        """The provider's key and base URL, however this version spells them."""
        key = (
            getattr(provider, "chosen_api_key", None)
            or getattr(provider, "api_key", None)
            or None
        )
        client = getattr(provider, "client", None)
        base = getattr(client, "base_url", None)
        if base is None:
            base = getattr(provider, "api_base", None) or getattr(provider, "base_url", None)
        return (str(key) if key else None, str(base) if base else None)

    # -- endpoints ---------------------------------------------------------

    async def chat_completions(self, request: web.Request) -> web.StreamResponse:
        provider = self._current()
        if provider is None:
            return self._unavailable("AstrBot has no chat provider configured")
        key, base = self._credential(provider)
        if not key or not base:
            return self._unavailable(
                f"provider {getattr(provider, 'id', '?')} has no key or base url"
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": {"message": "body was not JSON"}}, status=400)
        # The model is Max's business, but a vendor that does not know the id
        # answers with a 404 that reads like a network fault.  Overriding it
        # with the provider's own model keeps the request honest about what
        # will actually serve it.
        body["model"] = model_name_of(provider) or body.get("model")
        url = f"{base.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        if _debug_body():
            logger.info("[max_bridge] llm proxy request " + _summarise(body))
        session = self._session
        try:
            upstream = await session.post(
                url,
                json=body,
                headers=headers,
                allow_redirects=True,
            )
        except aiohttp.ClientError as exc:
            logger.warning(f"[max_bridge] llm proxy could not reach {url}: {exc!r}")
            return self._unavailable(f"upstream unreachable: {exc!r}")
        if upstream.status >= 400:
            detail = await upstream.text()
            logger.warning(f"[max_bridge] llm proxy got {upstream.status} from {url}: {detail[:300]}")
            # Passed through with its own status: a rate limit stays a rate
            # limit, so Max's retry policy sees what really happened.
            return web.Response(status=upstream.status, text=detail, content_type="application/json")
        # Streamed through untouched, including SSE framing.  A buffered relay
        # would turn every reply into a full-length pause.
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": upstream.headers.get("Content-Type", "text/event-stream")},
        )
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
        finally:
            upstream.release()
        await response.write_eof()
        return response

    async def models(self, request: web.Request) -> web.StreamResponse:
        provider = self._current()
        if provider is None:
            return self._unavailable("no provider")
        model = model_name_of(provider) or "unknown"
        ident = getattr(provider, "id", "provider")
        return web.json_response(
            {
                "object": "list",
                "data": [{"id": model, "object": "model", "owned_by": ident}],
            }
        )

    @staticmethod
    def _unavailable(reason: str) -> web.Response:
        logger.warning(f"[max_bridge] llm proxy unavailable: {reason}")
        return web.json_response(
            {"error": {"message": reason, "type": "bridge_unavailable"}},
            status=503,
        )