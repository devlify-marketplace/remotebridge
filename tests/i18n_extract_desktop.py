import ast, glob, os
FILES = ["host_p12.py", "viewer_p12.py", "feedback_cli.py"]
# strings passed to t() indirectly, or sent by the host/auth code and translated by lookup at the viewer
EXTRA = [
 "control", "view-only", "open", "resolved", "unknown error",
 "Control is held by another viewer",
 "Clipboard sync disabled by admin policy", "File transfer disabled by admin policy",
 "Chat disabled by admin policy", "Whiteboard disabled by admin policy",
 "Voice chat disabled by admin policy", "Remote printing disabled by admin policy",
 "Only the control viewer can print (view-only session)",
 "This host isn't connected to a support channel", "Malformed feedback request",
 "viewer ID is not on the whitelist", "incorrect unattended-access password",
 "missing or incorrect 2FA code", "unattended password (+2FA) verified",
 "one-time connection authorized via the admin console REST API",
 "accepted at host console", "declined or timed out at host console",
 "Viewer ID blocked by org policy", "Viewer ID is not on the org policy allow-list",
 "expected an auth request first", "malformed auth request",
]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def extract(base=os.path.join(ROOT, "desktop")):
    out = set(EXTRA)
    for f in FILES:
        with open(os.path.join(base, f), encoding='utf-8') as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "t" and node.args:
                a = node.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    out.add(a.value)
    return sorted(out)
if __name__ == "__main__":
    for i, s in enumerate(extract()): print(i, repr(s))
