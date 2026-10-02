"""Stable numeric identities for the QQ open platform's openids.

Max's OneBot edge parses ``user_id``/``group_id``/``message_id`` as decimal
integers (``OneBot.Types.parseIntId``) and derives a conversation's identity
from them.  The QQ open platform, by contrast, addresses every account and
group by an opaque openid.  Something has to translate, and it has to be
*stable*: a group that comes back as a different number after a restart is a
different conversation to Max, so its history, its member roster and its
dedupe keys all reset.

Users are mapped into a ten-digit band and groups into a plausible QQ group
number band.  Both avoid the negative range, which Max reserves for private
chats (``isPrivateChat`` in OneBot.Types: a group id is private when it is
negative and above -10^12).

Message ids are not mapped at all.  They are minted from the clock, because
Max orders a conversation by ``(occurred_at, message_id)``: two messages in
the same second have to keep the order they arrived in, and a hash of the
openid would not.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

# Where the two kinds of id live.  The bands are wide enough that collisions
# are rare and narrow enough that a human reading a log can tell at a glance
# which kind of conversation an id came from.
USER_BAND = (10_000_000_000, 10_999_999_999)
GROUP_BAND = (700_000_000, 799_999_999)

KIND_USER = "user"
KIND_GROUP = "group"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ids (
  kind TEXT NOT NULL,
  openid TEXT NOT NULL,
  num INTEGER NOT NULL,
  PRIMARY KEY (kind, openid),
  UNIQUE (kind, num)
);
CREATE TABLE IF NOT EXISTS counters (
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  ref TEXT PRIMARY KEY,
  num INTEGER NOT NULL UNIQUE
);
"""


class IdMap:
    """Bidirectional openid <-> integer map backed by sqlite.

    Both directions are indexed, so a reply coming back from Max resolves the
    conversation it belongs to in one query.  Allocation walks the band
    forward on the (rare) occasion a number is already taken; sqlite's write
    transaction is the only lock needed, since the plugin runs one loop.
    """

    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- openid -> number -------------------------------------------------

    def number(self, kind: str, openid: str) -> int:
        """The stable number for an openid, allocating one on first sight."""
        if not openid:
            raise ValueError("refusing to map an empty openid")
        row = self._db.execute(
            "SELECT num FROM ids WHERE kind = ? AND openid = ?", (kind, openid)
        ).fetchone()
        if row is not None:
            return int(row[0])
        low, high = USER_BAND if kind == KIND_USER else GROUP_BAND
        candidate = self._seed(kind, openid, low, high)
        while candidate <= high:
            taken = self._db.execute(
                "SELECT 1 FROM ids WHERE kind = ? AND num = ?", (kind, candidate)
            ).fetchone()
            if taken is None:
                self._db.execute(
                    "INSERT INTO ids (kind, openid, num) VALUES (?, ?, ?)",
                    (kind, openid, candidate),
                )
                return candidate
            candidate += 1
        raise RuntimeError(f"{kind} id band {(low, high)} is exhausted")

    @staticmethod
    def _seed(kind: str, openid: str, low: int, high: int) -> int:
        """Derive a starting point in the band from the openid itself."""
        digest = hashlib.blake2b(f"{kind}:{openid}".encode(), digest_size=8).digest()
        return low + int.from_bytes(digest, "big") % (high - low + 1)

    # -- number -> openid -------------------------------------------------

    def openid(self, kind: str, num: int) -> str | None:
        """The openid a number came from, or None when it is one of ours only."""
        try:
            wanted = int(num)
        except (TypeError, ValueError):
            return None
        row = self._db.execute(
            "SELECT openid FROM ids WHERE kind = ? AND num = ?", (kind, wanted)
        ).fetchone()
        return None if row is None else str(row[0])

    # -- message ids ------------------------------------------------------

    def message_id(self) -> int:
        """Mint a message id that sorts the way the messages arrived.

        ``seconds * 1000 + slot`` keeps ids monotonic within a second, which is
        the only case where Max's ``(time, message_id)`` ordering would
        otherwise tie.  The slot counter is persisted so a restart cannot hand
        out an id that predates the last message.
        """
        row = self._db.execute(
            "SELECT value FROM counters WHERE name = 'message_seq'"
        ).fetchone()
        sequence = (0 if row is None else int(row[0])) + 1
        self._db.execute(
            "INSERT INTO counters (name, value) VALUES ('message_seq', ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (sequence,),
        )
        return int(time.time()) * 1000 + sequence % 1000

    def remember_message(self, ref: str, num: int) -> None:
        """Tie a platform message reference to the number Max was given.

        Without this a quoted reply has to degrade to text: the reference QQ
        hands back is not the id Max knows, and inventing an unrelated one
        would attach the conversation to the wrong message.
        """
        self._db.execute(
            "INSERT INTO messages (ref, num) VALUES (?, ?) "
            "ON CONFLICT(ref) DO UPDATE SET num = excluded.num",
            (ref, num),
        )

    def message_number(self, ref: str) -> int | None:
        row = self._db.execute(
            "SELECT num FROM messages WHERE ref = ?", (ref,)
        ).fetchone()
        return None if row is None else int(row[0])

    def message_ref(self, num: int) -> str | None:
        try:
            wanted = int(num)
        except (TypeError, ValueError):
            return None
        row = self._db.execute(
            "SELECT ref FROM messages WHERE num = ?", (wanted,)
        ).fetchone()
        return None if row is None else str(row[0])