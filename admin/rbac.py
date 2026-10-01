"""
RemoteBridge RBAC & Centralized Authorization Module.
Provides fine-grained role-based access control, permission evaluation,
and resource-level access verification.
"""

from typing import Dict, List, Set, Optional, Any
import dbcompat

# Standard RemoteBridge Permission Definitions
PERMISSIONS: Dict[str, str] = {
    "user.read": "View user accounts and profiles",
    "user.create": "Create new user accounts",
    "user.update": "Update user accounts and credentials",
    "user.delete": "Delete user accounts",
    "role.read": "View system roles and permissions",
    "role.manage": "Manage roles, permissions, and user role assignments",
    "organization.read": "View organization details and members",
    "organization.manage": "Manage organization settings and memberships",
    "device.read": "View registered devices and device status",
    "device.create": "Enroll and register new devices",
    "device.update": "Update device properties, names, and groups",
    "device.delete": "Delete devices from the system",
    "device.manage": "Manage device policies, groups, and assignments",
    "device.revoke": "Revoke device relay access",
    "device.restore": "Restore device relay access",
    "device.wake": "Send Wake-on-LAN packets to devices",
    "device.connect": "Initiate remote connection to devices",
    "session.read": "View remote session history and session logs",
    "session.connect": "Establish active remote sessions",
    "session.terminate": "Terminate active remote sessions",
    "policy.read": "View policy groups and settings",
    "policy.manage": "Create, edit, or delete policy groups",
    "group.read": "View group definitions",
    "group.manage": "Create, edit, or delete groups",
    "audit.read": "View security and administrative audit logs",
    "audit.export": "Export audit log records",
    "api_key.read": "View API key list and metadata",
    "api_key.create": "Generate new API keys and assign scopes",
    "api_key.revoke": "Revoke API keys",
    "deployment.read": "View deployment releases and installer bundles",
    "deployment.manage": "Publish releases and update deployment configurations",
    "feedback.read": "View user feedback and support tickets",
    "feedback.manage": "Reply to and manage feedback tickets",
    "security.read": "View security settings and 2FA statuses",
    "security.manage": "Modify security settings and reset 2FA",
}

# Role Defaults
DEFAULT_ROLE_PERMISSIONS: Dict[str, List[str]] = {
    "admin": list(PERMISSIONS.keys()),
    "auditor": [
        "user.read", "role.read", "organization.read", "device.read",
        "session.read", "policy.read", "group.read", "audit.read",
        "api_key.read", "deployment.read", "feedback.read", "security.read"
    ],
    "user": [
        "user.read", "device.read", "device.create", "device.update",
        "device.connect", "session.read", "policy.read"
    ]
}


def _now_iso() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def seed_rbac(conn) -> None:
    """Ensure default roles, permissions, and role_permissions are seeded."""
    now = _now_iso()

    # 1. Seed Roles
    for role_name in ("admin", "auditor", "user"):
        conn.execute(
            """INSERT INTO roles (name, description, is_system, created_at)
               VALUES (?, ?, 1, ?)
               ON CONFLICT(name) DO NOTHING""",
            (role_name, f"System role {role_name}", now)
        )

    # 2. Seed Permissions
    for code, desc in PERMISSIONS.items():
        conn.execute(
            """INSERT INTO permissions (code, description, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(code) DO UPDATE SET description = excluded.description""",
            (code, desc, now)
        )

    # Fetch IDs
    roles_map = {row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM roles").fetchall()}
    perms_map = {row["code"]: row["id"] for row in conn.execute("SELECT id, code FROM permissions").fetchall()}

    # 3. Seed Role-Permissions
    for role_name, perm_codes in DEFAULT_ROLE_PERMISSIONS.items():
        role_id = roles_map.get(role_name)
        if not role_id:
            continue
        for code in perm_codes:
            perm_id = perms_map.get(code)
            if perm_id:
                conn.execute(
                    """INSERT INTO role_permissions (role_id, permission_id)
                       VALUES (?, ?)
                       ON CONFLICT DO NOTHING""",
                    (role_id, perm_id)
                )

    conn.commit()


def get_user_role(conn, user_id: int, organization_id: int = 1) -> str:
    """Retrieve the user's role name within an organization."""
    row = conn.execute(
        """SELECT r.name 
           FROM organization_members om
           JOIN roles r ON om.role_id = r.id
           WHERE om.user_id = ? AND om.organization_id = ? AND om.status = 'active'""",
        (user_id, organization_id)
    ).fetchone()
    if row:
        return row["name"]

    # Fallback to direct role column on users if organization_members record doesn't exist
    user_row = conn.execute("SELECT status FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user_row or user_row["status"] != "active":
        return "user"
    return "user"


def get_user_permissions(conn, user_id: int, organization_id: int = 1) -> Set[str]:
    """Get all permission codes granted to a user in an organization."""
    rows = conn.execute(
        """SELECT p.code 
           FROM organization_members om
           JOIN role_permissions rp ON om.role_id = rp.role_id
           JOIN permissions p ON rp.permission_id = p.id
           WHERE om.user_id = ? AND om.organization_id = ? AND om.status = 'active'""",
        (user_id, organization_id)
    ).fetchall()
    return {row["code"] for row in rows}


def has_permission(conn, user_id: int, permission_code: str, organization_id: int = 1) -> bool:
    """Check if a user possesses a specific permission."""
    perms = get_user_permissions(conn, user_id, organization_id)
    return permission_code in perms


def can_access_device(conn, user_id: int, device_id: str, organization_id: int = 1) -> bool:
    """Check if user can access a specific device."""
    role = get_user_role(conn, user_id, organization_id)
    if role in ("admin", "auditor"):
        return True
    
    # Check device organization and owner
    device = conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    if not device:
        return False
    
    dev_dict = dict(device)
    dev_org = dev_dict.get("organization_id") or 1
    if dev_org != organization_id:
        return False

    dev_owner = dev_dict.get("owner_user_id")
    if dev_owner and dev_owner != user_id:
        return False

    return has_permission(conn, user_id, "device.read", organization_id)


def can_manage_user(conn, actor_user_id: int, target_user_id: int, organization_id: int = 1) -> bool:
    """Check if an actor can manage (edit/delete/reset 2FA) a target user."""
    if actor_user_id == target_user_id:
        return True
    actor_role = get_user_role(conn, actor_user_id, organization_id)
    if actor_role != "admin":
        return False
    return has_permission(conn, actor_user_id, "user.update", organization_id)
