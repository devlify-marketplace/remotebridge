"""
Phase 12 - client side of the admin console's feedback channel.

Used by the host (to file/read tickets on its own behalf and on behalf of a
connected viewer) and by feedback_cli.py (a host operator at the keyboard).
All functions raise admin_client.AdminUnavailable with a readable message on
failure; none ever hang (short timeouts) or print.
"""

import admin_client


def submit(admin_url: str, report_token: str, message: str, category: str = "other",
           viewer_id: str = None, client_version: str = None) -> int:
    """Files a ticket. Returns its ticket_id."""
    body = {"message": message, "category": category}
    if viewer_id:
        body["viewer_id"] = viewer_id
    if client_version:
        body["client_version"] = client_version
    reply = admin_client._request(admin_url, "/api/v1/feedback", method="POST", body=body,
                                  token=report_token, timeout=8.0)
    return reply["ticket_id"]


def list_tickets(admin_url: str, report_token: str, viewer_id: str = None) -> list:
    """This device's tickets, newest first. With viewer_id, only that viewer's
    (see protocol_p12 for why)."""
    items = admin_client._request(admin_url, "/api/v1/feedback", token=report_token, timeout=8.0)
    if viewer_id is not None:
        items = [i for i in items if i.get("viewer_id") == viewer_id]
    else:
        items = [i for i in items if not i.get("viewer_id")]   # the host's own
    return items
