"""
Phase 9 - Policy resolution.

A device's *effective policy* is just its group's row, reshaped from
SQLite's 0/1 integers into the JSON booleans a host's admin_client.py
expects, plus that group's viewer allow/block lists. There's no
per-device override layer yet - one device, one group, one policy -
which keeps "why is this host behaving this way" a one-hop question an
admin can always answer by looking at the device's group. A richer
override model (device-level exceptions on top of the group) is a
reasonable Phase 9+ follow-up if a real deployment needs it.

This shape must stay in sync with desktop/admin_client.py's
DEFAULT_POLICY - that's the fallback a host uses when it can't reach
this console at all, and it needs to mean the same thing as the
permissive Default group does here.
"""

import store

_BOOL_FIELDS = {
    "allow_unattended_access", "allow_file_transfer", "allow_clipboard",
    "allow_chat", "allow_whiteboard", "allow_printing", "allow_voice",
    "require_view_only", "require_whitelist", "auto_update",
}


def group_to_policy(conn, group_row: dict) -> dict:
    allowed, blocked = store.get_group_viewer_lists(conn, group_row["id"])
    policy = {col: group_row[col] for col in store.POLICY_COLUMNS}
    for field in _BOOL_FIELDS:
        policy[field] = bool(policy[field])
    policy["group"] = group_row["name"]
    policy["allowed_viewer_ids"] = allowed
    policy["blocked_viewer_ids"] = blocked
    return policy


def resolve_policy_for_device(conn, device_row: dict) -> dict:
    group_row = store.get_group(conn, device_row["group_id"])
    if group_row is None:
        group_row = store.get_group_by_name(conn, store.DEFAULT_GROUP_NAME)
    return group_to_policy(conn, group_row)
