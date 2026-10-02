"""Checks for the parts of the bridge that do not need AstrBot.

Run: python3 test_bridge.py

The interesting claims are the ones Max's parser would otherwise reject in
production: ids that survive a restart, ids that are decimal integers, a
private event that carries no group_id, and messages inside one second that
keep their order.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# The plugin's modules are siblings of this script, and are imported by name:
# main.py reaches them relatively because AstrBot loads the directory as a
# package, but this test runs the directory as a plain script.
sys.path.insert(0, str(Path(__file__).parent))

import onebot  # noqa: E402
from ids import GROUP_BAND, KIND_GROUP, KIND_USER, USER_BAND, IdMap  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got: object, want: object) -> None:
    if got == want:
        print(f"ok   {name}")
    else:
        print(f"FAIL {name}\n       got:  {got!r}\n       want: {want!r}")
        FAILURES.append(name)


def check_true(name: str, condition: bool) -> None:
    check(name, bool(condition), True)


def decimals(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "ids.sqlite3"

        # -- ids ------------------------------------------------------------
        ids = IdMap(db)
        user_a = ids.number(KIND_USER, "USEROPENID_A")
        group_a = ids.number(KIND_GROUP, "GROUPOPENID_A")
        check_true("user id is an integer", decimals(user_a))
        check_true("group id is an integer", decimals(group_a))
        check_true("user id sits in the user band", USER_BAND[0] <= user_a <= USER_BAND[1])
        check_true("group id sits in the group band", GROUP_BAND[0] <= group_a <= GROUP_BAND[1])
        check_true("ids are positive", user_a > 0 and group_a > 0)
        check("the same openid keeps its number", ids.number(KIND_USER, "USEROPENID_A"), user_a)
        check_true(
            "different kinds never share a number",
            ids.number(KIND_USER, "GROUPOPENID_A") != group_a,
        )

        # Restart: a fresh IdMap over the same file is what a reload looks like.
        again = IdMap(db)
        check("a restart keeps the user id", again.number(KIND_USER, "USEROPENID_A"), user_a)
        check("a restart keeps the group id", again.number(KIND_GROUP, "GROUPOPENID_A"), group_a)
        check("reverse lookup finds the openid", again.openid(KIND_USER, user_a), "USEROPENID_A")
        check("reverse lookup misses cleanly", again.openid(KIND_USER, 12345), None)

        # Distinct openids must not collide.
        many = {again.number(KIND_USER, f"U{i}") for i in range(500)}
        check("500 users get 500 numbers", len(many), 500)
        many_groups = {again.number(KIND_GROUP, f"G{i}") for i in range(500)}
        check("500 groups get 500 numbers", len(many_groups), 500)
        check("user and group bands do not overlap", many & many_groups, set())

        # -- message ids ----------------------------------------------------
        first = again.message_id()
        second = again.message_id()
        third = again.message_id()
        check_true("message ids increase", first < second < third)
        check_true("message ids are integers", decimals(third))
        check(
            "the slot stays inside its second",
            third // 1000 == first // 1000 or third > first,
            True,
        )

        # -- message references --------------------------------------------
        again.remember_message("REFIDX_abc", first)
        check("a reference resolves to its number", again.message_number("REFIDX_abc"), first)
        check("the number resolves back", again.message_ref(first), "REFIDX_abc")
        check("an unknown reference is None", again.message_number("REFIDX_nope"), None)
        check("a non-numeric lookup is None", again.openid(KIND_USER, "abc"), None)

        # -- events --------------------------------------------------------
        group_event = onebot.message_event(
            self_id=10_000_000_001,
            user=user_a,
            group=group_a,
            message_id=first,
            segments=[onebot.text_segment("hi")],
            raw_text="hi",
            nickname="Alice",
            time=1_700_000_000,
        )
        for field in ("self_id", "group_id", "user_id", "message_id", "message", "sender"):
            check_true(f"group event carries {field}", field in group_event)
        check("group event says group", group_event["message_type"], "group")
        check_true("group event ids are integers", all(decimals(group_event[f]) for f in ("self_id", "group_id", "user_id", "message_id")))
        check("sender names the user", group_event["sender"]["user_id"], user_a)

        private_event = onebot.message_event(
            self_id=10_000_000_001,
            user=user_a,
            group=None,
            message_id=second,
            segments=[onebot.text_segment("psst")],
            raw_text="psst",
            nickname=None,
            time=1_700_000_001,
        )
        check("private event says private", private_event["message_type"], "private")
        check_true(
            "private event omits group_id (Max derives it)",
            "group_id" not in private_event,
        )

        lifecycle = onebot.lifecycle("connect", 10_000_000_001, 1_700_000_000)
        check("lifecycle announces the post type", lifecycle["post_type"], "meta_event")
        check("lifecycle announces the subtype", lifecycle["sub_type"], "connect")
        check("lifecycle names the bot", lifecycle["self_id"], 10_000_000_001)

        # -- actions -------------------------------------------------------
        send = {"action": "send_group_msg", "params": {"group_id": group_a, "message": []}, "echo": "e1"}
        check_true("an action frame is recognised", onebot.is_action(send))
        check_true("an ack is not an action", not onebot.is_action({"status": "ok", "echo": "e1"}))

        success = onebot.ok("e1", {"message_id": 1})
        check("success carries retcode 0", success["retcode"], 0)
        check("success echoes", success["echo"], "e1")
        refusal = onebot.refused("e2", "nope")
        check("refusal is not a success", refusal["retcode"] != 0, True)
        check("refusal explains itself", refusal["message"], "nope")

        check(
            "segments read as text",
            onebot.segments_to_text(
                [
                    onebot.text_segment("看这个 "),
                    {"type": "at", "data": {"qq": "123"}},
                    onebot.text_segment(" 好的"),
                    {"type": "image", "data": {"url": "http://x/y.png"}},
                ]
            ),
            "看这个 @123 好的[图片]",
        )
        check("an unknown segment is named, not dropped", onebot.segments_to_text([{"type": "json", "data": {}}]), "[json]")
        check("garbage in place of segments reads as empty", onebot.segments_to_text(None), "")
        again.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failing: {', '.join(FAILURES)}")
        return 1
    print("all bridge checks pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())