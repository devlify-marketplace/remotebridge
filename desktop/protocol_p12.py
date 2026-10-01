"""
Phase 12 - in-app support/feedback over the control channel.

A viewer doesn't talk to the admin console directly (it isn't enrolled with
it and holds no credential for it) - it asks the HOST it is connected to,
which is enrolled, to file a ticket or read tickets back. Two messages:

  MSG_FEEDBACK         viewer -> host   {"action": "submit", "category", "message"}
                                        {"action": "list"}
  MSG_FEEDBACK_RESULT  host -> viewer   {"ok": bool, "error"?: str,
                                         "ticket_id"?: int, "items"?: [...]}

The host stamps the viewer's ID onto every ticket it files on that viewer's
behalf, and for "list" returns only tickets carrying that same viewer ID, so
one viewer can't read another viewer's tickets just because they share a host.
(The viewer ID is whatever the viewer claimed at auth - the same trust level
the rest of the session already gives it; this is a convenience filter, not a
security boundary against a viewer who deliberately impersonates another ID.)

An older host (<= Phase 11) doesn't know MSG_FEEDBACK and ignores it; an
older viewer never sends it. Nothing breaks - the new viewer's /feedback just
reports that it got no answer.
"""

import json

MSG_FEEDBACK = 0x82
MSG_FEEDBACK_RESULT = 0x83

MAX_MESSAGE_CHARS = 4000


def pack_feedback_submit(category: str, message: str) -> bytes:
    return json.dumps({"action": "submit", "category": category, "message": message}).encode("utf-8")


def pack_feedback_list() -> bytes:
    return json.dumps({"action": "list"}).encode("utf-8")


def unpack_feedback(payload: bytes) -> dict:
    data = json.loads(payload.decode("utf-8"))
    if data.get("action") not in ("submit", "list"):
        raise ValueError("unknown feedback action")
    if data["action"] == "submit":
        data["message"] = str(data.get("message", ""))[:MAX_MESSAGE_CHARS]
        data["category"] = str(data.get("category", "other"))[:20]
    return data


def pack_feedback_result(ok: bool, error: str = "", ticket_id=None, items=None) -> bytes:
    out = {"ok": bool(ok)}
    if error:
        out["error"] = error
    if ticket_id is not None:
        out["ticket_id"] = ticket_id
    if items is not None:
        out["items"] = items
    return json.dumps(out).encode("utf-8")


def unpack_feedback_result(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))
