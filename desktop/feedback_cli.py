#!/usr/bin/env python3
"""
Phase 12 - send support/feedback to your org's admins from the host machine,
and read their replies.

    python3 feedback_cli.py send "The screen goes black after I lock the PC" --category bug
    python3 feedback_cli.py replies

Uses the same admin_enrollment.json the host wrote when it enrolled, so the host
must have been started once with --admin-url first. Messages are in whatever
language you type; the admin console shows them as written.
"""

import argparse
import sys

import admin_client
import i18n
import updater
from i18n import t
import support


def _load(args):
    entry = admin_client.load_enrollment(args.enrollment_file)
    deploy = admin_client.load_deploy_config(args.deploy_config)
    admin_url = args.admin_url or deploy.get("admin_url")
    if not entry or not entry.get("report_token") or not admin_url:
        sys.exit(t("This host isn't enrolled with an admin console yet. Start the host once with "
                   "--admin-url and --enrollment-key first."))
    return admin_url, entry["report_token"]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Send feedback to, or read replies from, your org's admins")
    p.add_argument("--admin-url")
    p.add_argument("--enrollment-file", default=admin_client.ENROLLMENT_FILE)
    p.add_argument("--deploy-config", default=admin_client.DEPLOY_CONFIG_FILE)
    p.add_argument("--lang", help="interface language code, e.g. es")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("message")
    s.add_argument("--category", choices=["bug", "question", "idea", "other"], default="other")
    sub.add_parser("replies")
    args = p.parse_args(argv)
    if args.lang:
        i18n.set_language(args.lang)

    admin_url, token = _load(args)
    try:
        if args.cmd == "send":
            ticket = support.submit(admin_url, token, args.message, args.category,
                                    client_version=updater.CURRENT_VERSION)
            print(t("Thanks - your feedback was sent (ticket #{id}).", id=ticket))
        else:
            items = support.list_tickets(admin_url, token)
            if not items:
                print(t("No feedback sent from this host yet."))
            for item in items:
                print(f"#{item['id']} [{t(item['status'])}] {item['message'][:80]}")
                if item.get("reply"):
                    print("   " + t("Reply: {text}", text=item["reply"]))
    except admin_client.AdminUnavailable as e:
        print(t("Couldn't reach the admin console: {error}", error=e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
