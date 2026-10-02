"""The OneBot 11 dialect Max speaks, as far as the bridge needs it.

Everything here is shaped by what ``OneBot.Event`` and ``OneBot.Action``
actually parse, not by the general OneBot 11 spec:

* ``user_id``/``group_id``/``message_id`` must be decimal integers or the
  whole event is rejected (``parseIntId``).
* A private message carries no ``group_id``: Max derives the pseudo group id
  itself (``privateChatGroupId``), which is what makes ``isPrivateChat`` hold.
* A group message needs ``self_id``, ``group_id``, ``user_id``,
  ``message_id``, ``message`` (segments) and ``sender``.
* ``sender`` is optional per field but not as an object; ``nickname`` and
  ``card`` are read when present.

Actions are answered with ``{status, retcode, data, echo}``.  ``retcode`` 0 is
only ever returned for work that actually happened: an action this bridge
cannot perform reports 100 with a reason, because a fake success would make
Max record a delivery that never happened.
"""

from __future__ import annotations

from typing import Any

# Segment types Max understands on the way in (``OneBot.Segment``).
TEXT = "text"
AT = "at"
IMAGE = "image"
REPLY = "reply"

# Actions Max may issue (``OneBot.Action.actionName``).  Kept as a set so an
# unrecognised one is answered with a refusal instead of a guess.
SEND_GROUP_MSG = "send_group_msg"
SEND_PRIVATE_MSG = "send_private_msg"
GET_GROUP_MEMBER_LIST = "get_group_member_list"
GET_GROUP_INFO = "get_group_info"
GET_FORWARD_MSG = "get_forward_msg"
GET_GROUP_FILE_URL = "get_group_file_url"
UPLOAD_GROUP_FILE = "upload_group_file"
UPLOAD_PRIVATE_FILE = "upload_private_file"
GET_GROUP_MSG_HISTORY = "get_group_msg_history"
GET_FRIEND_MSG_HISTORY = "get_friend_msg_history"
SET_MSG_EMOJI_LIKE = "set_msg_emoji_like"
SEND_POKE = "send_poke"
SET_FRIEND_ADD_REQUEST = "set_friend_add_request"

SUPPORTED_ACTIONS = frozenset(
    {SEND_GROUP_MSG, SEND_PRIVATE_MSG, GET_GROUP_MEMBER_LIST}
)


def text_segment(text: str) -> dict[str, Any]:
    return {"type": TEXT, "data": {"text": text}}


def at_segment(user: int) -> dict[str, Any]:
    return {"type": AT, "data": {"qq": str(user)}}


def image_segment(url: str) -> dict[str, Any]:
    # Inbound OneBot carries the URL under "url"; "file" is the outbound key.
    return {"type": IMAGE, "data": {"url": url, "file": url}}


def reply_segment(message_id: int) -> dict[str, Any]:
    return {"type": REPLY, "data": {"id": str(message_id)}}


def lifecycle(sub_type: str, self_id: int, now: int | None = None) -> dict[str, Any]:
    """The meta event Max turns into "the client is connected".

    ``OneBot.Server`` publishes the connected client only on this event, and
    the platform's endpoint registration hangs off it, so a bridge that
    skipped it would have inbound events with nowhere to land.
    """
    stamp = int(now if now is not None else 0)
    return {
        "time": stamp,
        "self_id": self_id,
        "post_type": "meta_event",
        "meta_event_type": "lifecycle",
        "sub_type": sub_type,
    }


def sender(user: int, nickname: str | None, card: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"user_id": user}
    if nickname:
        out["nickname"] = nickname
    if card:
        out["card"] = card
    return out


def message_event(
    *,
    self_id: int,
    user: int,
    message_id: int,
    segments: list[dict[str, Any]],
    raw_text: str,
    nickname: str | None,
    time: int,
    group: int | None = None,
) -> dict[str, Any]:
    """A group or private message event.

    ``group=None`` means private: Max synthesises the pseudo group id from the
    user id, so sending one here would be redundant at best and wrong at
    worst.
    """
    event: dict[str, Any] = {
        "time": time,
        "self_id": self_id,
        "post_type": "message",
        "message_type": "group" if group is not None else "private",
        "user_id": user,
        "message_id": message_id,
        "message": segments,
        "raw_message": raw_text,
        "sender": sender(user, nickname),
    }
    if group is not None:
        event["group_id"] = group
    return event


def ok(echo: Any, data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "retcode": 0, "data": data if data is not None else {}, "echo": echo}


def refused(echo: Any, reason: str) -> dict[str, Any]:
    """A refusal Max will record as a real failure rather than a phantom success."""
    return {"status": "failed", "retcode": 100, "message": reason, "echo": echo}


def is_action(frame: dict[str, Any]) -> bool:
    """Frames from Max carry @action@; anything else is an ack or a notice."""
    return isinstance(frame, dict) and "action" in frame


def segments_to_text(segments: Any) -> str:
    """Flatten segments the way a person would read them.

    Max sends text, mentions, images and the occasional segment type this
    bridge has no meaning for.  Anything unrecognised is named rather than
    dropped, so a lost face or forward card shows up in the transcript
    instead of vanishing.
    """
    if not isinstance(segments, list):
        return ""
    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        kind = segment.get("type")
        data = segment.get("data") or {}
        if kind == TEXT:
            parts.append(str(data.get("text", "")))
        elif kind == AT:
            parts.append(f"@{data.get('qq', '')}")
        elif kind == IMAGE:
            parts.append("[图片]")
        elif kind == REPLY:
            parts.append("[引用]")
        elif kind:
            parts.append(f"[{kind}]")
    return "".join(parts)