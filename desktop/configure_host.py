"""
Phase 2 - host configuration CLI.

Manages host_config.json: the unattended-access password, 2FA, and the
viewer-ID whitelist. Run this on the machine that will act as host,
before (or between) `python3 host.py` sessions.

Usage:
    python3 configure_host.py show
    python3 configure_host.py set-password
    python3 configure_host.py clear-password
    python3 configure_host.py enable-2fa
    python3 configure_host.py disable-2fa
    python3 configure_host.py whitelist-add <viewer-id>
    python3 configure_host.py whitelist-remove <viewer-id>
    python3 configure_host.py set-timeout <seconds>
"""

import argparse
import getpass
import sys

import auth


def cmd_show(args) -> None:
    config = auth.load_config(args.config)
    print("Host access-control config:")
    print(f"  Unattended password set : {bool(config.get('unattended_password_hash'))}")
    print(f"  2FA enabled             : {config.get('totp_enabled')}")
    wl = config.get("whitelist") or []
    print(f"  Whitelist               : {wl if wl else '(empty - any viewer ID may attempt a connection)'}")
    print(f"  Confirmation timeout    : {config.get('confirmation_timeout_seconds')}s")


def cmd_set_password(args) -> None:
    config = auth.load_config(args.config)
    pw = getpass.getpass("New unattended-access password: ")
    if not pw:
        sys.exit("Password cannot be empty.")
    pw2 = getpass.getpass("Confirm: ")
    if pw != pw2:
        sys.exit("Passwords do not match.")
    salt, digest = auth.hash_password(pw)
    config["unattended_password_salt"] = salt
    config["unattended_password_hash"] = digest
    auth.save_config(config, args.config)
    print("Unattended-access password set.")
    print("Connections without this password (or with a wrong one) still fall back to a console confirmation prompt.")


def cmd_clear_password(args) -> None:
    config = auth.load_config(args.config)
    config["unattended_password_hash"] = None
    config["unattended_password_salt"] = None
    if config.get("totp_enabled"):
        config["totp_enabled"] = False
        config["totp_secret"] = None
        print("Note: 2FA was enabled and required this password - it has been disabled too.")
    auth.save_config(config, args.config)
    print("Unattended-access password cleared. All connections now require manual confirmation.")


def cmd_enable_2fa(args) -> None:
    config = auth.load_config(args.config)
    if not config.get("unattended_password_hash"):
        sys.exit("Set an unattended-access password first (set-password) - 2FA is a second factor, not a replacement for one.")
    try:
        secret = auth.generate_totp_secret()
    except ImportError:
        sys.exit("Missing dependency 'pyotp'. Install with: pip install pyotp")
    config["totp_secret"] = secret
    config["totp_enabled"] = True
    auth.save_config(config, args.config)

    import pyotp
    uri = pyotp.TOTP(secret).provisioning_uri(name="remote-desktop-host", issuer_name="RemoteDesktopApp")
    print("2FA enabled. Add this secret to an authenticator app (Google Authenticator, Authy, etc.):")
    print(f"  Secret: {secret}")
    print(f"  Or use this URI (most apps accept manual/URI entry): {uri}")
    print("The viewer will need to pass the current 6-digit code with --totp on every unattended connection.")


def cmd_disable_2fa(args) -> None:
    config = auth.load_config(args.config)
    config["totp_enabled"] = False
    config["totp_secret"] = None
    auth.save_config(config, args.config)
    print("2FA disabled.")


def cmd_whitelist_add(args) -> None:
    config = auth.load_config(args.config)
    if args.viewer_id not in config["whitelist"]:
        config["whitelist"].append(args.viewer_id)
    auth.save_config(config, args.config)
    print(f"Whitelist: {config['whitelist']}")


def cmd_whitelist_remove(args) -> None:
    config = auth.load_config(args.config)
    config["whitelist"] = [v for v in config["whitelist"] if v != args.viewer_id]
    auth.save_config(config, args.config)
    print(f"Whitelist: {config['whitelist']}")


def cmd_set_timeout(args) -> None:
    config = auth.load_config(args.config)
    config["confirmation_timeout_seconds"] = args.seconds
    auth.save_config(config, args.config)
    print(f"Confirmation prompt will now auto-reject after {args.seconds}s.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure Phase 2 host security settings")
    parser.add_argument("--config", default=auth.CONFIG_PATH, help="path to host_config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show").set_defaults(func=cmd_show)
    sub.add_parser("set-password").set_defaults(func=cmd_set_password)
    sub.add_parser("clear-password").set_defaults(func=cmd_clear_password)
    sub.add_parser("enable-2fa").set_defaults(func=cmd_enable_2fa)
    sub.add_parser("disable-2fa").set_defaults(func=cmd_disable_2fa)

    p_add = sub.add_parser("whitelist-add")
    p_add.add_argument("viewer_id")
    p_add.set_defaults(func=cmd_whitelist_add)

    p_remove = sub.add_parser("whitelist-remove")
    p_remove.add_argument("viewer_id")
    p_remove.set_defaults(func=cmd_whitelist_remove)

    p_timeout = sub.add_parser("set-timeout")
    p_timeout.add_argument("seconds", type=int)
    p_timeout.set_defaults(func=cmd_set_timeout)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
