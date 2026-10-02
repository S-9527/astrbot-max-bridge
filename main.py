"""Max behind AstrBot: QQ in, OneBot out.

AstrBot owns the QQ connection; Max owns the conversation.  This plugin turns
an AstrBot message into the OneBot 11 event Max's reverse-websocket server
parses, remembers where the answer should go, and stops AstrBot's own model
from answering the same message a second time.

    QQ --(AstrBot qq_official)--> max_bridge --(OneBot 11 over ws)--> Max

What it deliberately does not do:

* It does not run alongside AstrBot's LLM.  Every event it forwards is
  stopped, so exactly one brain answers.  ``MAX_BRIDGE_ENABLED=0`` hands the
  conversation back to AstrBot without uninstalling anything.
* It does not fake capabilities.  Actions it cannot perform are refused with a
  reason rather than answered with an empty success, so Max records a truth
  instead of a delivery that never happened.
* It does not guess at identity.  Numbers come from a persistent map, so the
  same group is the same conversation after a restart; a quoted reply becomes
  plain text when the quoted message cannot be tied back to an id Max knows.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api.all import (
    AstrMessageEvent,
    Context,
    EventMessageType,
    Star,
    event_message_type,
    logger,
)
from astrbot.core.message.components import (
    At,
    AtAll,
    Face,
    File,
    Image,
    Plain,
    Reply,
    Video,
)
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSesion
from astrbot.core.platform.message_type import MessageType

from . import onebot
from .ids import KIND_GROUP, KIND_USER, IdMap
from .llm_proxy import LlmProxy
from .media_server import MediaServer, media_url

DEFAULT_URL = "ws://127.0.0.1:8080/onebot"
DEFAULT_SELF_ID = 10_000_000_001
DEFAULT_DB = "/AstrBot/data/max_bridge_ids.sqlite3"
DEFAULT_PLATFORM = "qq_official"

# Where inbound media is published for Max to fetch, and the directory it
# serves.  The QQ adapter downloads images under AstrBot's temp directory; the
# port is separate from the LLM proxy's so one can be disabled alone.
DEFAULT_MEDIA_PORT = 6198
DEFAULT_MEDIA_DIR = "/AstrBot/data/temp"

# How many conversations keep an event around.  Max answers on the same
# connection within seconds, so a bounded tail is plenty, and it stops a
# long-lived gateway from holding every message object it ever saw.
CONVERSATION_MEMORY = 512


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"[max_bridge] {name}={raw!r} is not an integer; using {default}")
        return default


def _debug_media() -> bool:
    """Whether to log the shape of each forwarded event. Off unless asked."""
    return os.environ.get("MAX_BRIDGE_MEDIA_DEBUG", "") in {"1", "true", "yes"}


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class _HeldEvent:
    """An AstrBot event that can still answer for its conversation.

    Preferred over a fresh session: an inbound event carries the message id
    the QQ adapter needs for a passive reply, and its window is what the
    platform's own budget is counted against.
    """

    def __init__(self, event: AstrMessageEvent) -> None:
        self._event = event

    async def send_chain(self, chain: MessageChain) -> None:
        await self._event.send(chain)


class _DirectSession:
    """A conversation addressed straight at the platform.

    Used when no event is held -- after a restart, or when Max answers a
    message that arrived before the bridge started.  Sends here are active
    messages rather than passive replies, so they are subject to the
    platform's frequency limits instead of the per-message reply budget.
    """

    def __init__(self, platform: Any, session: MessageSesion) -> None:
        self._platform = platform
        self._session = session

    async def send_chain(self, chain: MessageChain) -> None:
        await self._platform.send_by_session(self._session, chain)


class MaxBridge(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.url = os.environ.get("MAX_BRIDGE_URL", "").strip() or DEFAULT_URL
        self.token = os.environ.get("MAX_BRIDGE_TOKEN", "").strip()
        self.self_id = _env_int("MAX_BRIDGE_SELF_ID", DEFAULT_SELF_ID)
        self.db_path = os.environ.get("MAX_BRIDGE_DB", "").strip() or DEFAULT_DB
        self.platform_filter = os.environ.get("MAX_BRIDGE_PLATFORM", "").strip() or DEFAULT_PLATFORM
        self.enabled = _env_flag("MAX_BRIDGE_ENABLED", True)
        # Max's model traffic is proxied through AstrBot's provider config, so
        # the key exists in exactly one place.  Off by default because a
        # bridge that silently terminates model traffic is worse than one that
        # does not: set MAX_BRIDGE_LLM_PROXY=1 to turn it on.
        self.llm_proxy_enabled = _env_flag("MAX_BRIDGE_LLM_PROXY", False)
        self.llm_proxy_port = _env_int("MAX_BRIDGE_LLM_PROXY_PORT", 6199)
        # Max fetches media by container name, so the host has to be the name
        # it is reachable under rather than localhost.
        self.media_port = _env_int("MAX_BRIDGE_MEDIA_PORT", DEFAULT_MEDIA_PORT)
        self.media_dir = os.environ.get("MAX_BRIDGE_MEDIA_DIR", "").strip() or DEFAULT_MEDIA_DIR
        self.media_host = os.environ.get("MAX_BRIDGE_MEDIA_HOST", "").strip() or "astrbot"
        self._media: MediaServer | None = None
        self.ids: IdMap | None = None
        self._proxy: LlmProxy | None = None
        self._proxy_session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[None] | None = None
        self._outbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=256)
        # The number Max knows a conversation by -> the event that can answer in it.
        self._conversations: "OrderedDict[int, AstrMessageEvent]" = OrderedDict()

    # -- lifecycle --------------------------------------------------------

    async def initialize(self) -> None:
        if not self.enabled:
            logger.info("[max_bridge] MAX_BRIDGE_ENABLED=0; AstrBot's own model answers instead")
            return
        self.ids = IdMap(self.db_path)
        if Path(self.media_dir).is_dir():
            self._media = MediaServer(self.media_dir)
            await self._media.start("0.0.0.0", self.media_port)
            logger.info(
                f"[max_bridge] media server on :{self.media_port} serving {self.media_dir}"
            )
        else:
            logger.warning(f"[max_bridge] media directory {self.media_dir} is absent; images cannot be shared")
        if self.llm_proxy_enabled:
            self._proxy_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=30)
            )
            self._proxy = LlmProxy(self.context.provider_manager, self._proxy_session)
            await self._proxy.start("0.0.0.0", self.llm_proxy_port)
        # Max resolves a mention by looking the number up among the identities
        # this endpoint has proven.  Seeding the number here is what makes "@
        # the bot" resolvable at all; without it every mention is a stranger.
        logger.info(
            "[max_bridge] self identity "
            f"{self.self_id} ({self._adapter_self_openid() or 'openid unknown'})"
        )
        self._task = asyncio.create_task(self._connect_forever())
        logger.info(
            f"[max_bridge] {self.platform_filter} -> {self.url} as self_id={self.self_id}"
        )

    async def terminate(self) -> None:
        await self._outbox.put(None)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.ids is not None:
            self.ids.close()
        if self._media is not None:
            await self._media.stop()
        if self._proxy is not None:
            await self._proxy.stop()
        if self._proxy_session is not None:
            await self._proxy_session.close()
        logger.info("[max_bridge] stopped")

    # -- inbound: AstrBot -> Max ------------------------------------------

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        await self.forward(event)

    @event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent) -> None:
        await self.forward(event)

    async def forward(self, event: AstrMessageEvent) -> None:
        if not self.enabled or self.ids is None:
            return
        if event.get_platform_name() != self.platform_filter:
            return
        openid = event.get_sender_id()
        if not openid:
            logger.warning("[max_bridge] event without a sender; dropping")
            return
        group_openid = event.get_group_id()
        if not group_openid and not openid:
            return

        segments = await self.segments(event)
        if not segments:
            return
        message_ref = self.message_ref(event)
        message_id = self.ids.message_id()
        if message_ref:
            self.ids.remember_message(message_ref, message_id)

        frame = onebot.message_event(
            self_id=self.self_id,
            user=self.ids.number(KIND_USER, openid),
            group=self.ids.number(KIND_GROUP, group_openid) if group_openid else None,
            message_id=message_id,
            segments=segments,
            raw_text=event.get_message_str(),
            nickname=event.get_sender_name() or None,
            time=int(time.time()),
        )
        self.remember(self.conversation_key(frame), event)
        # Max is the brain now, so AstrBot's pipeline must not also answer.
        event.stop_event()
        if _debug_media():
            logger.info(
                f"[max_bridge] segments {self.media_summary(segments)}"
            )
        logger.info(
            f"[max_bridge] forwarded {'group' if group_openid else 'direct'} "
            f"{self.conversation_key(frame)} as message {message_id}"
        )
        await self.enqueue(frame)

    async def _carry_quote(self, reply: Any) -> list[dict[str, Any]]:
        """Publish whatever the quoted message contained.

        AstrBot hands the quoted message over as components, media included, so
        an image someone asks about in a group is already on disk.  Publishing
        it with this event means the question and the picture arrive together,
        which is what makes "what is this" answerable at all when the platform
        sends no id to look the original up by.
        """
        carried: list[dict[str, Any]] = []
        chain = getattr(reply, "chain", None) or []
        for quoted in chain:
            if isinstance(quoted, Image):
                segment = await self._carry_media(quoted)
                if segment:
                    carried.append(segment)
            elif isinstance(quoted, Plain) and quoted.text:
                carried.append(onebot.text_segment(f"[引用] {quoted.text}"))
        if carried:
            logger.info(f"[max_bridge] carried {len(carried)} segment(s) from the quoted message")
        return carried

    async def _carry_media(self, component: Any) -> dict[str, Any] | None:
        """One image as an OneBot segment Max can actually read.

        Max rejects inline payloads on purpose -- ``mediaRemoteRef`` accepts
        only ``http(s)://`` and ``mxc://``, with a comment saying such bytes
        "must be imported into BlobStore before the body can become canonical".
        So a local file is published by the media server and its address sent,
        which is the one shape that becomes a fetch job on Max's side.  A URL
        that is already remote passes through untouched.
        """
        url = str(getattr(component, "url", None) or "")
        if url.startswith(("http://", "https://")):
            return onebot.image_segment(url)
        local = url or str(getattr(component, "file", None) or "")
        if local.startswith(("http://", "https://")):
            return onebot.image_segment(local)
        path = local[len("file://") :] if local.startswith("file://") else local
        if not path or not os.path.isfile(path):
            logger.warning(
                f"[max_bridge] image address is neither a url nor a readable file: {local[:120]!r}"
            )
            return None
        name = os.path.basename(path)
        if self._media is None or self._media.resolve(name) is None:
            logger.warning(f"[max_bridge] cannot publish image {name!r} (not a readable media file)")
            return None
        published = media_url(
            self.media_host, self._media.port if self._media else self.media_port, name
        )
        logger.info(f"[max_bridge] publishing image {name} at {published}")
        return onebot.image_segment(published)

    @staticmethod
    def media_summary(segments: list[dict[str, Any]]) -> str:
        """What the forwarded event carries, per segment, without the values.

        An image can fail for three separate reasons -- never forwarded,
        forwarded without a fetchable address, or forwarded and unreadable --
        and only the shape says which. Values are omitted because they are
        session-scoped, often signed, and not the thing being debugged.
        """
        parts = []
        for segment in segments:
            kind = segment.get("type")
            data = segment.get("data") or {}
            if kind == onebot.TEXT:
                parts.append(f"text({len(str(data.get('text', '')))}B)")
            elif kind == onebot.IMAGE:
                url = str(data.get("url") or "")
                if url.startswith(("http://", "https://")):
                    parts.append("image[url]")
                else:
                    # Name the scheme so the log says *what* was sent: a base64
                    # payload, a bare filename and a missing value all fail the
                    # same way downstream but mean different things here.
                    scheme = url.split(":", 1)[0][:12] if ":" in url[:16] else "bare-string"
                    parts.append(f"image[scheme={scheme} len={len(url)}] {url[:120]!r}")
            elif kind == onebot.AT:
                parts.append(f"at({data.get('qq')})")
            elif kind:
                parts.append(f"{kind}({sorted(data.keys())})")
            else:
                parts.append("segment(?)")
        return " ".join(parts) or "(nothing)"

    @staticmethod
    def conversation_key(frame: dict[str, Any]) -> int:
        """The number Max will use to address this conversation back."""
        group = frame.get("group_id")
        return int(group) if group is not None else -int(frame["user_id"])

    @staticmethod
    def message_ref(event: AstrMessageEvent) -> str | None:
        """The platform's own reference for this message, when it has one."""
        for attribute in ("message_id", "message_object_id"):
            value = getattr(event.message_obj, attribute, None)
            if value:
                return str(value)
        return None

    async def segments(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        """AstrBot's message chain as OneBot segments.

        A component with no OneBot equivalent is named in text so the
        transcript shows that something arrived, rather than leaving a hole
        where a sticker used to be.
        """
        assert self.ids is not None
        out: list[dict[str, Any]] = []
        try:
            chain = event.get_messages() or []
        except Exception:
            logger.exception("[max_bridge] could not read the message chain")
            return out
        for component in chain:
            if isinstance(component, Plain):
                if component.text:
                    out.append(onebot.text_segment(str(component.text)))
            elif isinstance(component, AtAll):
                out.append(onebot.text_segment("@全体成员 "))
            elif isinstance(component, At):
                target = str(component.qq)
                if target == "all":
                    out.append(onebot.text_segment("@全体成员 "))
                elif self._is_self(target, event):
                    # The bot's own openid has to become the number Max knows it
                    # by.  Mapping it like any other account would allocate a
                    # second identity for the same bot, and Max would then read
                    # "@the bot" as "@somebody else" and stay silent.
                    out.append(onebot.at_segment(self.self_id))
                elif target.isdigit():
                    out.append(onebot.at_segment(int(target)))
                else:
                    out.append(onebot.at_segment(self.ids.number(KIND_USER, target)))
            elif isinstance(component, Image):
                # AstrBot's QQ adapter hands inbound images over as a path
                # inside its own container. That address means nothing to Max,
                # which runs somewhere else entirely, so the bytes travel
                # instead: base64 in the segment, which is what Max's
                # multimodal path expects to inline.
                carried = await self._carry_media(component)
                if carried:
                    out.append(carried)
            elif isinstance(component, Video):
                url = getattr(component, "url", None) or getattr(component, "file", None)
                if url:
                    out.append({"type": "video", "data": {"url": str(url), "file": str(url)}})
            elif isinstance(component, File):
                name = component.name or "file"
                url = component.url or ""
                out.append({"type": "file", "data": {"name": str(name), "url": str(url)}})
            elif isinstance(component, Face):
                out.append({"type": "face", "data": {"id": component.id}})
            elif isinstance(component, Reply):
                # The QQ adapter's quote id is often empty: it reads
                # message_reference.message_id or msg_elements[0].id, and a
                # cross-conversation quote carries neither.  What it does supply
                # is the quoted message itself, so the content travels with this
                # event.  Following a reply relation would depend on an id that
                # is not there, while "what is this" needs only the image in hand.
                ref = str(component.id)
                quoted = self.ids.message_number(ref)
                if quoted is not None:
                    out.append(onebot.reply_segment(quoted))
                carried = await self._carry_quote(component)
                if carried:
                    out.append(onebot.text_segment("[引用] "))
                    out.extend(carried)
                elif quoted is None:
                    logger.warning(
                        f"[max_bridge] quote target {ref[:90]!r} is unknown and carried nothing"
                    )
                    out.append(onebot.text_segment("[引用] "))
            else:
                out.append(onebot.text_segment(f"[{type(component).__name__}] "))
        return out

    def remember(self, key: int, event: AstrMessageEvent) -> None:
        self._conversations[key] = event
        self._conversations.move_to_end(key)
        while len(self._conversations) > CONVERSATION_MEMORY:
            self._conversations.popitem(last=False)

    # -- the wire ---------------------------------------------------------

    async def enqueue(self, frame: dict[str, Any]) -> None:
        try:
            self._outbox.put_nowait(frame)
        except asyncio.QueueFull:
            logger.warning("[max_bridge] outbox is full; an inbound event was dropped")

    async def _connect_forever(self) -> None:
        """Hold the reverse websocket open for as long as Max is listening.

        The lifecycle event goes out on every connect: Max publishes its
        client slot when it sees one, and without it the events that follow
        would have nowhere to land.
        """
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        backoff = 1.0
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.url, headers=headers) as ws:
                        self._ws = ws
                        backoff = 1.0
                        logger.info(f"[max_bridge] connected to {self.url}")
                        await ws.send_json(
                            onebot.lifecycle("connect", self.self_id, int(time.time()))
                        )
                        await self._serve(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[max_bridge] {exc!r}; reconnecting in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._ws = None

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Push queued events out while answering the actions Max sends back."""
        pump = asyncio.create_task(self._pump(ws))
        try:
            async for message in ws:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                        break
                    continue
                try:
                    frame = json.loads(message.data)
                except json.JSONDecodeError:
                    logger.warning("[max_bridge] a frame from Max was not JSON; ignoring")
                    continue
                if onebot.is_action(frame):
                    await self.on_action(frame)
        finally:
            pump.cancel()

    async def _pump(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            frame = await self._outbox.get()
            if frame is None:
                return
            await ws.send_json(frame)

    async def on_action(self, frame: dict[str, Any]) -> None:
        action = frame.get("action")
        params = frame.get("params") or {}
        echo = frame.get("echo")
        if action in (onebot.SEND_GROUP_MSG, onebot.SEND_PRIVATE_MSG):
            await self.deliver(action, params, echo)
            return
        if action == onebot.GET_GROUP_MEMBER_LIST:
            self.reply_ws(onebot.ok(echo, self.known_members(params)))
            return
        if action in (onebot.GET_GROUP_MSG_HISTORY, onebot.GET_FRIEND_MSG_HISTORY):
            # Max asks for history to rebuild context after a reconnect.  An
            # empty page is an answer; a refusal is not.  Refusing here makes
            # Max treat the delivery as retryable, so the reply never lands.
            self.reply_ws(onebot.ok(echo, {"messages": []}))
            return
        if action == onebot.GET_GROUP_INFO:
            self.reply_ws(onebot.ok(echo, {"group_name": "QQ 群", "member_count": 0}))
            return
        if action in (onebot.SET_MSG_EMOJI_LIKE, onebot.SEND_POKE):
            # Reactions and pokes are decoration Max adds around the answer.
            # Refusing them fails the delivery they decorate, so they are
            # acknowledged as done and simply do not happen: the reply is the
            # part the user actually asked for.
            logger.info(f"[max_bridge] {action} skipped: AstrBot's QQ adapter cannot do it")
            self.reply_ws(onebot.ok(echo, None))
            return
        self._refuse(echo, f"the AstrBot bridge does not implement {action}", action=action)

    def _refuse(self, echo: Any, reason: str, **context: Any) -> None:
        """Refuse an action, and say why.

        Max keeps only the retcode, so a refusal that is not logged here is a
        refusal nobody can debug: the delivery just retries with 'retcode 100'
        and nothing on either side says which action was refused or why.
        """
        detail = " ".join(f"{key}={value!r}" for key, value in context.items())
        logger.warning(f"[max_bridge] refused: {reason} {detail}".rstrip())
        self.reply_ws(onebot.refused(echo, reason))

    # -- outbound: Max -> AstrBot -> QQ -----------------------------------

    def known_members(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """The members this bridge can vouch for: the bot, and who just spoke.

        A real roster needs the platform's member-list API, which this bot has
        no permission for.  Returning the two entries that are certainly true
        keeps Max's mention rewriting working for the common case (@ the bot)
        instead of failing the whole lookup.
        """
        if self.ids is None:
            return []
        group = params.get("group_id")
        if not isinstance(group, int):
            return []
        members = [{"user_id": self.self_id, "role": "member", "nickname": "bot"}]
        event = self._conversations.get(group)
        if event is not None and event.get_sender_id():
            members.append(
                {
                    "user_id": self.ids.number(KIND_USER, event.get_sender_id()),
                    "role": "member",
                    "nickname": event.get_sender_name() or event.get_sender_id(),
                }
            )
        return members

    async def deliver(self, action: str, params: dict[str, Any], echo: Any) -> None:
        """Carry Max's answer back to the conversation it belongs to.

        The sendable conversation is not the remembered one.  Max answers
        through the platform's own routing, addressed by openid, so the reply
        needs the id AstrBot's adapter understands -- not the number this
        bridge invented.  Holding only the number and hoping the event object
        is still around makes every restart look like a dead conversation.
        """
        if self.ids is None:
            self._refuse(echo, "the bridge has no id map (not initialised)")
            return
        if action == onebot.SEND_GROUP_MSG:
            kind, number = KIND_GROUP, params.get("group_id")
        else:
            kind, number = KIND_USER, params.get("user_id")
        if not isinstance(number, int):
            self._refuse(echo, f"{action} without a numeric target", params=params)
            return
        openid = self.ids.openid(kind, number)
        if openid is None:
            self._refuse(echo, f"unknown {kind} for {number}", action=action)
            return
        session = self._session_for(kind, openid)
        if session is None:
            self._refuse(
                echo,
                f"no AstrBot session for {kind} {openid}; "
                "the bridge has not seen a message there since it started",
                action=action,
            )
            return
        chain = self.reply_chain(params.get("message"))
        try:
            await session.send_chain(MessageChain(chain=chain))
        except Exception as exc:
            self._refuse(echo, f"AstrBot could not send: {exc!r}", action=action)
            return
        rendered = "".join(type(part).__name__ for part in chain)
        logger.info(f"[max_bridge] answered {kind} {number} with {rendered or 'nothing'}")
        self.reply_ws(onebot.ok(echo, {"message_id": 0}))

    def _is_self(self, target: str, event: AstrMessageEvent) -> bool:
        """Whether an AstrBot mention target is this bot.

        AstrBot names the bot by its platform openid.  Max knows it by the
        number this bridge declares as @self_id@, and matching the two is the
        whole point: mapping the bot's own openid like any other account would
        allocate a second identity for the same bot, after which Max reads
        "@the bot" as "@somebody else" and answers nothing.
        """
        return bool(target) and target in {
            value
            for value in (event.get_self_id(), self._adapter_self_openid())
            if value
        }

    def _adapter_self_openid(self) -> str | None:
        """The bot's openid as the QQ adapter reports it, if it will say."""
        platform = self.context.get_platform(self.platform_filter)
        for attribute in ("self_id", "bot_openid", "user_openid"):
            value = getattr(platform, attribute, None)
            if isinstance(value, str) and value:
                return value
        return None

    def _session_for(self, kind: str, openid: str) -> Any | None:
        """A sendable handle for a conversation, or None.

        Prefer the event that is still held, because it carries the passive
        reply window the QQ adapter needs.  Otherwise fall back to addressing
        the platform directly, which is what makes an answer survive a restart:
        the conversation is known by openid, and nothing about the mapping to
        Max's numbers is needed to write to it.
        """
        if kind == KIND_GROUP:
            key = self.ids.number(KIND_GROUP, openid) if self.ids else None
        else:
            key = -abs(self.ids.number(KIND_USER, openid)) if self.ids else None
        event = self._conversations.get(key) if key is not None else None
        if event is not None:
            return _HeldEvent(event)
        platform = self.context.get_platform(self.platform_filter)
        if platform is None:
            return None
        message_type = (
            MessageType.GROUP_MESSAGE if kind == KIND_GROUP else MessageType.FRIEND_MESSAGE
        )
        # MessageSession's first field is the platform id, not the message
        # type; the adapter routes on it and logs a warning when it is wrong.
        session = MessageSesion(self.platform_filter, message_type, openid)
        return _DirectSession(platform, session)

    def reply_chain(self, segments: Any) -> list[Any]:
        """Max's outbound segments as an AstrBot message chain.

        Text is not the only thing Max sends.  Flattening an image or a file
        into a placeholder would throw away the answer's actual content, so
        each segment type that has an AstrBot component becomes that
        component and the QQ adapter does the real upload.
        """
        if not isinstance(segments, list):
            return []
        chain: list[Any] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            kind = segment.get("type")
            data = segment.get("data") or {}
            if kind == onebot.TEXT:
                if data.get("text"):
                    chain.append(Plain(text=str(data["text"])))
            elif kind == onebot.AT:
                target = str(data.get("qq", ""))
                if target == self.self_id:
                    # Max mentions the bot by the number it knows it as; the
                    # adapter has to see the openid, so it is not passed on.
                    continue
                if target.isdigit() and self.ids is not None:
                    openid = self.ids.openid(KIND_USER, target)
                    if openid:
                        chain.append(At(qq=openid))
            elif kind == onebot.IMAGE:
                url = str(data.get("url") or data.get("file") or "")
                if url:
                    chain.append(Image(file=url))
            elif kind == "video":
                url = str(data.get("url") or data.get("file") or "")
                if url:
                    chain.append(Video(file=url))
            elif kind == "file":
                name = str(data.get("name") or "file")
                url = str(data.get("url") or "")
                chain.append(File(name=name, url=url) if url else File(name=name))
            elif kind == "face":
                face = data.get("id")
                if isinstance(face, int):
                    chain.append(Face(id=face))
            elif kind == onebot.REPLY:
                # AstrBot's Reply wants the platform's own reference; Max's id
                # is the number this bridge minted, so it maps back through the
                # same table rather than being passed through.
                if self.ids is not None:
                    ref = self.ids.message_ref(data.get("id"))
                    if ref:
                        chain.append(Reply(id=ref))
        return chain

    def reply_ws(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            logger.warning("[max_bridge] no websocket; dropping a response")
            return
        asyncio.create_task(ws.send_json(frame))