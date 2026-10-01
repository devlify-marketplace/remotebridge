"""
RemoteBridge Marketplace Storage & Business Logic Layer.
Handles marketplace profiles, categories, services, availability, orders,
messages, reviews, favorites, reports, notifications, payouts, fee calculations,
and RemoteBridge single-use session tokens.
"""

import json
import re
import secrets
import time
from datetime import datetime, timezone
import dbcompat
import store
from store import get_conn, _now_iso, IntegrityError

# --- Helper Utilities ---

def slugify(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    return text.strip('-') or "service"

def generate_order_number() -> str:
    prefix = "RB-ORD"
    rand_num = secrets.randbelow(899999) + 100000
    return f"{prefix}-{rand_num}"

# --- Platform Fee Calculations ---

def get_platform_fee_config(conn) -> dict:
    pct = float(store.get_setting(conn, "marketplace_fee_pct", "10.0"))
    fixed = float(store.get_setting(conn, "marketplace_fee_fixed", "0.0"))
    min_fee = float(store.get_setting(conn, "marketplace_fee_min", "0.0"))
    max_fee = float(store.get_setting(conn, "marketplace_fee_max", "1000.0"))
    return {"percentage": pct, "fixed_fee": fixed, "min_fee": min_fee, "max_fee": max_fee}

def set_platform_fee_config(conn, percentage: float, fixed_fee: float, min_fee: float, max_fee: float):
    store.set_setting(conn, "marketplace_fee_pct", str(percentage))
    store.set_setting(conn, "marketplace_fee_fixed", str(fixed_fee))
    store.set_setting(conn, "marketplace_fee_min", str(min_fee))
    store.set_setting(conn, "marketplace_fee_max", str(max_fee))

def calculate_fees(conn, amount: float) -> tuple:
    cfg = get_platform_fee_config(conn)
    raw_fee = (amount * (cfg["percentage"] / 100.0)) + cfg["fixed_fee"]
    fee = max(cfg["min_fee"], min(cfg["max_fee"], raw_fee))
    fee = round(min(fee, amount), 2)
    provider_amt = round(amount - fee, 2)
    return fee, provider_amt

# --- Categories ---

def list_categories(conn, active_only: bool = False) -> list:
    sql = "SELECT * FROM marketplace_categories"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY name ASC"
    return [dict(r) for r in conn.execute(sql).fetchall()]

def get_category_by_id(conn, cat_id: int):
    row = conn.execute("SELECT * FROM marketplace_categories WHERE id = ?", (cat_id,)).fetchone()
    return dict(row) if row else None

def get_category_by_slug(conn, slug: str):
    row = conn.execute("SELECT * FROM marketplace_categories WHERE slug = ?", (slug,)).fetchone()
    return dict(row) if row else None

def create_category(conn, name: str, slug: str = None, description: str = None, icon: str = None) -> int:
    slug = slugify(slug or name)
    cur = conn.execute(
        "INSERT INTO marketplace_categories (name, slug, description, icon, active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
        (name.strip(), slug, description or "", icon or "wrench", _now_iso())
    )
    conn.commit()
    return cur.lastrowid

def update_category(conn, cat_id: int, name: str, description: str = None, icon: str = None, active: int = 1):
    conn.execute(
        "UPDATE marketplace_categories SET name = ?, description = ?, icon = ?, active = ? WHERE id = ?",
        (name.strip(), description or "", icon or "wrench", 1 if active else 0, cat_id)
    )
    conn.commit()

# --- Profiles / Providers ---

def create_provider_profile(conn, user_id: int, display_name: str, username: str, profile_photo: str = None,
                            headline: str = None, bio: str = None, country: str = None, city: str = None,
                            languages: list = None, skills: list = None) -> int:
    now = _now_iso()
    langs_json = json.dumps(languages or ["English"])
    skills_json = json.dumps(skills or ["Remote Desktop Support"])
    clean_username = slugify(username or display_name)
    cur = conn.execute(
        """INSERT INTO marketplace_profiles
           (user_id, display_name, username, profile_photo, headline, bio, country, city, languages, skills,
            verification_status, provider_status, average_rating, review_count, completed_jobs, response_time, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unverified', 'active', 0.0, 0, 0, '1 hour', ?, ?)""",
        (user_id, display_name, clean_username, profile_photo, headline or "", bio or "", country or "", city or "",
         langs_json, skills_json, now, now)
    )
    conn.commit()
    return cur.lastrowid

def get_provider_profile(conn, profile_id_or_user_id: int):
    row = conn.execute("SELECT * FROM marketplace_profiles WHERE id = ? OR user_id = ?",
                       (profile_id_or_user_id, profile_id_or_user_id)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["languages"] = json.loads(d["languages"] or "[]")
    d["skills"] = json.loads(d["skills"] or "[]")
    return d

def get_provider_profile_by_username(conn, username: str):
    row = conn.execute("SELECT * FROM marketplace_profiles WHERE username = ?", (username,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["languages"] = json.loads(d["languages"] or "[]")
    d["skills"] = json.loads(d["skills"] or "[]")
    return d

def update_provider_profile(conn, profile_id: int, display_name: str = None, headline: str = None, bio: str = None,
                            country: str = None, city: str = None, languages: list = None, skills: list = None,
                            profile_photo: str = None):
    p = get_provider_profile(conn, profile_id)
    if not p:
        raise ValueError("Profile not found")
    display_name = display_name or p["display_name"]
    headline = headline if headline is not None else p["headline"]
    bio = bio if bio is not None else p["bio"]
    country = country if country is not None else p["country"]
    city = city if city is not None else p["city"]
    profile_photo = profile_photo if profile_photo is not None else p["profile_photo"]
    langs_json = json.dumps(languages) if languages is not None else json.dumps(p["languages"])
    skills_json = json.dumps(skills) if skills is not None else json.dumps(p["skills"])
    conn.execute(
        """UPDATE marketplace_profiles
           SET display_name = ?, headline = ?, bio = ?, country = ?, city = ?, profile_photo = ?,
               languages = ?, skills = ?, updated_at = ?
           WHERE id = ?""",
        (display_name, headline, bio, country, city, profile_photo, langs_json, skills_json, _now_iso(), profile_id)
    )
    conn.commit()

def set_provider_verification(conn, profile_id: int, status: str):
    if status not in ("unverified", "pending", "verified", "rejected", "suspended"):
        raise ValueError("Invalid verification status")
    conn.execute("UPDATE marketplace_profiles SET verification_status = ?, updated_at = ? WHERE id = ?",
                 (status, _now_iso(), profile_id))
    conn.commit()

def list_providers(conn, verified_only: bool = False, country: str = None, search: str = None, limit: int = 20, offset: int = 0) -> list:
    clauses, params = [], []
    if verified_only:
        clauses.append("verification_status = 'verified'")
    if country:
        clauses.append("LOWER(country) = ?")
        params.append(country.lower())
    if search:
        clauses.append("(LOWER(display_name) LIKE ? OR LOWER(headline) LIKE ? OR LOWER(skills) LIKE ?)")
        term = f"%{search.lower()}%"
        params.extend([term, term, term])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    rows = conn.execute(f"SELECT * FROM marketplace_profiles {where} ORDER BY average_rating DESC, completed_jobs DESC LIMIT ? OFFSET ?", params).fetchall()
    res = []
    for r in rows:
        d = dict(r)
        d["languages"] = json.loads(d["languages"] or "[]")
        d["skills"] = json.loads(d["skills"] or "[]")
        res.append(d)
    return res

# --- Services ---

def create_service(conn, provider_id: int, category_id: int, title: str, description: str, price: float,
                   currency: str = "USD", pricing_type: str = "fixed", estimated_minutes: int = 60,
                   requirements: str = None, skills: list = None, slug: str = None) -> int:
    base_slug = slugify(slug or title)
    final_slug = base_slug
    idx = 1
    while get_service_by_slug(conn, final_slug) is not None:
        final_slug = f"{base_slug}-{idx}"
        idx += 1
    now = _now_iso()
    cur = conn.execute(
        """INSERT INTO marketplace_services
           (provider_id, category_id, title, slug, description, price, currency, pricing_type, estimated_minutes,
            requirements, active, featured, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)""",
        (provider_id, category_id, title.strip(), final_slug, description.strip(), float(price), currency.upper(),
         pricing_type, int(estimated_minutes), requirements or "", now, now)
    )
    service_id = cur.lastrowid
    if skills:
        for sk in set(skills):
            if sk.strip():
                conn.execute("INSERT INTO marketplace_service_skills (service_id, skill) VALUES (?, ?)",
                             (service_id, sk.strip()))
    conn.commit()
    return service_id

def get_service(conn, service_id: int):
    row = conn.execute("""
        SELECT s.*, c.name AS category_name, c.slug AS category_slug, p.display_name AS provider_name,
               p.username AS provider_username, p.profile_photo AS provider_photo, p.verification_status,
               p.average_rating AS provider_rating, p.review_count AS provider_reviews
        FROM marketplace_services s
        JOIN marketplace_categories c ON c.id = s.category_id
        JOIN marketplace_profiles p ON p.id = s.provider_id
        WHERE s.id = ?
    """, (service_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    skills = conn.execute("SELECT skill FROM marketplace_service_skills WHERE service_id = ?", (service_id,)).fetchall()
    d["skills"] = [s["skill"] for s in skills]
    return d

def get_service_by_slug(conn, slug: str):
    row = conn.execute("""
        SELECT s.*, c.name AS category_name, c.slug AS category_slug, p.display_name AS provider_name,
               p.username AS provider_username, p.profile_photo AS provider_photo, p.verification_status,
               p.average_rating AS provider_rating, p.review_count AS provider_reviews
        FROM marketplace_services s
        JOIN marketplace_categories c ON c.id = s.category_id
        JOIN marketplace_profiles p ON p.id = s.provider_id
        WHERE s.slug = ?
    """, (slug,)).fetchone()
    if not row:
        return None
    d = dict(row)
    skills = conn.execute("SELECT skill FROM marketplace_service_skills WHERE service_id = ?", (d["id"],)).fetchall()
    d["skills"] = [s["skill"] for s in skills]
    return d

def update_service(conn, service_id: int, title: str = None, category_id: int = None, description: str = None,
                   price: float = None, currency: str = None, pricing_type: str = None, estimated_minutes: int = None,
                   requirements: str = None, skills: list = None):
    s = get_service(conn, service_id)
    if not s:
        raise ValueError("Service not found")
    title = title.strip() if title else s["title"]
    category_id = category_id if category_id is not None else s["category_id"]
    description = description.strip() if description else s["description"]
    price = float(price) if price is not None else s["price"]
    currency = currency.upper() if currency else s["currency"]
    pricing_type = pricing_type if pricing_type else s["pricing_type"]
    estimated_minutes = int(estimated_minutes) if estimated_minutes is not None else s["estimated_minutes"]
    requirements = requirements if requirements is not None else s["requirements"]
    conn.execute(
        """UPDATE marketplace_services
           SET title = ?, category_id = ?, description = ?, price = ?, currency = ?, pricing_type = ?,
               estimated_minutes = ?, requirements = ?, updated_at = ?
           WHERE id = ?""",
        (title, category_id, description, price, currency, pricing_type, estimated_minutes, requirements, _now_iso(), service_id)
    )
    if skills is not None:
        conn.execute("DELETE FROM marketplace_service_skills WHERE service_id = ?", (service_id,))
        for sk in set(skills):
            if sk.strip():
                conn.execute("INSERT INTO marketplace_service_skills (service_id, skill) VALUES (?, ?)",
                             (service_id, sk.strip()))
    conn.commit()

def set_service_active(conn, service_id: int, active: bool):
    conn.execute("UPDATE marketplace_services SET active = ?, updated_at = ? WHERE id = ?",
                 (1 if active else 0, _now_iso(), service_id))
    conn.commit()

def set_service_featured(conn, service_id: int, featured: bool):
    conn.execute("UPDATE marketplace_services SET featured = ?, updated_at = ? WHERE id = ?",
                 (1 if featured else 0, _now_iso(), service_id))
    conn.commit()

def search_services(conn, query: str = None, category_id: int = None, min_price: float = None, max_price: float = None,
                    min_rating: float = None, verified_only: bool = False, country: str = None, pricing_type: str = None,
                    provider_id: int = None, active_only: bool = True, sort_by: str = "relevance",
                    limit: int = 20, offset: int = 0) -> tuple:
    clauses, params = [], []
    if active_only:
        clauses.append("s.active = 1")
    if provider_id:
        clauses.append("s.provider_id = ?")
        params.append(provider_id)
    if category_id:
        clauses.append("s.category_id = ?")
        params.append(category_id)
    if min_price is not None:
        clauses.append("s.price >= ?")
        params.append(float(min_price))
    if max_price is not None:
        clauses.append("s.price <= ?")
        params.append(float(max_price))
    if min_rating is not None:
        clauses.append("p.average_rating >= ?")
        params.append(float(min_rating))
    if verified_only:
        clauses.append("p.verification_status = 'verified'")
    if country:
        clauses.append("LOWER(p.country) = ?")
        params.append(country.lower())
    if pricing_type:
        clauses.append("s.pricing_type = ?")
        params.append(pricing_type)
    if query:
        q = f"%{query.lower()}%"
        clauses.append("(LOWER(s.title) LIKE ? OR LOWER(s.description) LIKE ? OR LOWER(p.display_name) LIKE ?)")
        params.extend([q, q, q])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    order_sql = "ORDER BY s.featured DESC, s.created_at DESC"
    if sort_by == "rating":
        order_sql = "ORDER BY p.average_rating DESC, p.review_count DESC"
    elif sort_by == "price_asc":
        order_sql = "ORDER BY s.price ASC"
    elif sort_by == "price_desc":
        order_sql = "ORDER BY s.price DESC"
    elif sort_by == "newest":
        order_sql = "ORDER BY s.created_at DESC"
    elif sort_by == "completed":
        order_sql = "ORDER BY p.completed_jobs DESC"

    count_sql = f"SELECT COUNT(*) FROM marketplace_services s JOIN marketplace_profiles p ON p.id = s.provider_id {where}"
    total = conn.execute(count_sql, params).fetchone()[0]

    sql = f"""
        SELECT s.*, c.name AS category_name, c.slug AS category_slug, p.display_name AS provider_name,
               p.username AS provider_username, p.profile_photo AS provider_photo, p.verification_status,
               p.average_rating AS provider_rating, p.review_count AS provider_reviews
        FROM marketplace_services s
        JOIN marketplace_categories c ON c.id = s.category_id
        JOIN marketplace_profiles p ON p.id = s.provider_id
        {where}
        {order_sql}
        LIMIT ? OFFSET ?
    """
    exec_params = list(params) + [limit, offset]
    rows = conn.execute(sql, exec_params).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        skills = conn.execute("SELECT skill FROM marketplace_service_skills WHERE service_id = ?", (d["id"],)).fetchall()
        d["skills"] = [sk["skill"] for sk in skills]
        items.append(d)
    return items, total

# --- Availability ---

def set_provider_availability(conn, provider_id: int, slots: list):
    conn.execute("DELETE FROM marketplace_availability WHERE provider_id = ?", (provider_id,))
    for s in slots:
        conn.execute(
            """INSERT INTO marketplace_availability (provider_id, day_of_week, start_time, end_time, timezone, active)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (provider_id, int(s["day_of_week"]), s["start_time"], s["end_time"], s.get("timezone", "UTC"), 1 if s.get("active", 1) else 0)
        )
    conn.commit()

def get_provider_availability(conn, provider_id: int) -> list:
    rows = conn.execute("SELECT * FROM marketplace_availability WHERE provider_id = ? ORDER BY day_of_week ASC, start_time ASC", (provider_id,)).fetchall()
    return [dict(r) for r in rows]

# --- Orders ---

def create_order(conn, customer_id: int, provider_id: int, service_id: int, title: str, description: str,
                 amount: float, currency: str = "USD", device_id: str = None, scheduled_at: str = None) -> dict:
    order_num = generate_order_number()
    fee, provider_amt = calculate_fees(conn, amount)
    now = _now_iso()
    cur = conn.execute(
        """INSERT INTO marketplace_orders
           (customer_id, provider_id, service_id, order_number, title, description, device_id, amount, currency,
            platform_fee, provider_amount, status, payment_status, scheduled_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'unpaid', ?, ?, ?)""",
        (customer_id, provider_id, service_id, order_num, title.strip(), description.strip(), device_id,
         amount, currency.upper(), fee, provider_amt, scheduled_at, now, now)
    )
    order_id = cur.lastrowid
    conn.commit()

    # Create system notification for provider
    provider_prof = get_provider_profile(conn, provider_id)
    if provider_prof:
        create_notification(conn, provider_prof["user_id"], "new_request",
                            "New Service Request", f"You received a request for '{title}'", related_order_id=order_id)
    return get_order(conn, order_id)

def get_order(conn, order_id_or_num):
    if isinstance(order_id_or_num, int) or str(order_id_or_num).isdigit():
        row = conn.execute("""
            SELECT o.*, s.slug AS service_slug, p.display_name AS provider_name, p.username AS provider_username,
                   p.user_id AS provider_user_id, u.username AS customer_username
            FROM marketplace_orders o
            JOIN marketplace_services s ON s.id = o.service_id
            JOIN marketplace_profiles p ON p.id = o.provider_id
            JOIN admin_users u ON u.id = o.customer_id
            WHERE o.id = ?
        """, (int(order_id_or_num),)).fetchone()
    else:
        row = conn.execute("""
            SELECT o.*, s.slug AS service_slug, p.display_name AS provider_name, p.username AS provider_username,
                   p.user_id AS provider_user_id, u.username AS customer_username
            FROM marketplace_orders o
            JOIN marketplace_services s ON s.id = o.service_id
            JOIN marketplace_profiles p ON p.id = o.provider_id
            JOIN admin_users u ON u.id = o.customer_id
            WHERE o.order_number = ?
        """, (str(order_id_or_num),)).fetchone()
    return dict(row) if row else None

def list_orders(conn, customer_id: int = None, provider_id: int = None, status: str = None, limit: int = 50, offset: int = 0) -> list:
    clauses, params = [], []
    if customer_id:
        clauses.append("o.customer_id = ?")
        params.append(customer_id)
    if provider_id:
        clauses.append("o.provider_id = ?")
        params.append(provider_id)
    if status:
        clauses.append("o.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    rows = conn.execute(f"""
        SELECT o.*, s.slug AS service_slug, p.display_name AS provider_name, u.username AS customer_username
        FROM marketplace_orders o
        JOIN marketplace_services s ON s.id = o.service_id
        JOIN marketplace_profiles p ON p.id = o.provider_id
        JOIN admin_users u ON u.id = o.customer_id
        {where} ORDER BY o.id DESC LIMIT ? OFFSET ?
    """, params).fetchall()
    return [dict(r) for r in rows]

def update_order_status(conn, order_id: int, new_status: str, actor_id: int = None) -> dict:
    o = get_order(conn, order_id)
    if not o:
        raise ValueError("Order not found")
    valid_statuses = ("pending", "accepted", "rejected", "scheduled", "in_progress", "completed", "cancelled", "disputed")
    if new_status not in valid_statuses:
        raise ValueError("Invalid order status")
    now = _now_iso()
    extra_field = ""
    if new_status == "completed":
        extra_field = ", completed_at = ?"
    elif new_status == "cancelled":
        extra_field = ", cancelled_at = ?"
    elif new_status == "disputed":
        extra_field = ", disputed_at = ?"
    elif new_status == "in_progress":
        extra_field = ", started_at = ?"

    if extra_field:
        conn.execute(f"UPDATE marketplace_orders SET status = ?, updated_at = ? {extra_field} WHERE id = ?",
                     (new_status, now, now, order_id))
    else:
        conn.execute("UPDATE marketplace_orders SET status = ?, updated_at = ? WHERE id = ?",
                     (new_status, now, order_id))
    conn.commit()

    if new_status == "completed":
        conn.execute("""
            UPDATE marketplace_profiles
            SET completed_jobs = completed_jobs + 1, updated_at = ?
            WHERE id = ?
        """, (now, o["provider_id"]))
        conn.commit()
        create_payout(conn, o["provider_id"], o["id"], o["provider_amount"], o["currency"])

    notify_target = o["customer_id"] if actor_id == o["provider_user_id"] else o["provider_user_id"]
    create_notification(conn, notify_target, f"order_{new_status}",
                        f"Order {new_status.replace('_', ' ').capitalize()}",
                        f"Order #{o['order_number']} status updated to {new_status}", related_order_id=order_id)

    return get_order(conn, order_id)

def update_order_payment_status(conn, order_id: int, payment_status: str, reference: str = None) -> dict:
    if payment_status not in ("unpaid", "pending", "paid", "refunded", "failed"):
        raise ValueError("Invalid payment status")
    now = _now_iso()
    conn.execute("UPDATE marketplace_orders SET payment_status = ?, updated_at = ? WHERE id = ?",
                 (payment_status, now, order_id))
    conn.commit()
    o = get_order(conn, order_id)
    if payment_status == "paid":
        create_notification(conn, o["customer_id"], "payment_confirmed", "Payment Confirmed",
                            f"Payment for order #{o['order_number']} confirmed.", related_order_id=order_id)
        create_notification(conn, o["provider_user_id"], "payment_confirmed", "Payment Received",
                            f"Payment for order #{o['order_number']} has been secured.", related_order_id=order_id)
    return o

# --- Messages ---

def add_order_message(conn, order_id: int, sender_id: int, message: str, attachment_metadata: dict = None) -> int:
    o = get_order(conn, order_id)
    if not o:
        raise ValueError("Order not found")
    if sender_id not in (o["customer_id"], o["provider_user_id"]):
        u = store.get_admin_user(conn, sender_id)
        if not u or u["role"] != "admin":
            raise PermissionError("User is not authorized to message on this order")
    cur = conn.execute(
        """INSERT INTO marketplace_messages (order_id, sender_id, message, attachment_metadata, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (order_id, sender_id, message.strip(), json.dumps(attachment_metadata) if attachment_metadata else None, _now_iso())
    )
    conn.commit()

    receiver_id = o["customer_id"] if sender_id == o["provider_user_id"] else o["provider_user_id"]
    create_notification(conn, receiver_id, "new_message", "New Order Message",
                        f"New message on order #{o['order_number']}", related_order_id=order_id)

    return cur.lastrowid

def list_order_messages(conn, order_id: int) -> list:
    rows = conn.execute("""
        SELECT m.*, u.username AS sender_username
        FROM marketplace_messages m
        JOIN admin_users u ON u.id = m.sender_id
        WHERE m.order_id = ?
        ORDER BY m.created_at ASC
    """, (order_id,)).fetchall()
    res = []
    for r in rows:
        d = dict(r)
        d["attachment_metadata"] = json.loads(d["attachment_metadata"]) if d["attachment_metadata"] else None
        res.append(d)
    return res

# --- Reviews ---

def create_review(conn, order_id: int, customer_id: int, rating: int, review_text: str) -> int:
    o = get_order(conn, order_id)
    if not o:
        raise ValueError("Order not found")
    if o["customer_id"] != customer_id:
        raise PermissionError("Only the customer of this order can leave a review")
    if o["status"] != "completed":
        raise ValueError("Reviews can only be left on completed orders")
    if not (1 <= int(rating) <= 5):
        raise ValueError("Rating must be between 1 and 5")
    cur = conn.execute(
        """INSERT INTO marketplace_reviews (order_id, customer_id, provider_id, rating, review, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (order_id, customer_id, o["provider_id"], int(rating), review_text.strip(), _now_iso())
    )
    conn.commit()

    provider_id = o["provider_id"]
    stats = conn.execute("""
        SELECT COUNT(*) AS cnt, AVG(rating) AS avg_r
        FROM marketplace_reviews WHERE provider_id = ?
    """, (provider_id,)).fetchone()
    avg_r = round(float(stats["avg_r"] or 0.0), 1)
    cnt = int(stats["cnt"] or 0)
    conn.execute("UPDATE marketplace_profiles SET average_rating = ?, review_count = ? WHERE id = ?",
                 (avg_r, cnt, provider_id))
    conn.commit()

    create_notification(conn, o["provider_user_id"], "new_review", "New Review Received",
                        f"You received a {rating}-star review on order #{o['order_number']}", related_order_id=order_id)
    return cur.lastrowid

def list_provider_reviews(conn, provider_id: int, limit: int = 20, offset: int = 0) -> list:
    rows = conn.execute("""
        SELECT r.*, u.username AS customer_username, o.title AS order_title
        FROM marketplace_reviews r
        JOIN admin_users u ON u.id = r.customer_id
        JOIN marketplace_orders o ON o.id = r.order_id
        WHERE r.provider_id = ?
        ORDER BY r.created_at DESC LIMIT ? OFFSET ?
    """, (provider_id, limit, offset)).fetchall()
    return [dict(r) for r in rows]

def delete_review(conn, review_id: int):
    rev = conn.execute("SELECT * FROM marketplace_reviews WHERE id = ?", (review_id,)).fetchone()
    if rev:
        provider_id = rev["provider_id"]
        conn.execute("DELETE FROM marketplace_reviews WHERE id = ?", (review_id,))
        conn.commit()
        stats = conn.execute("SELECT COUNT(*) AS cnt, AVG(rating) AS avg_r FROM marketplace_reviews WHERE provider_id = ?", (provider_id,)).fetchone()
        avg_r = round(float(stats["avg_r"] or 0.0), 1)
        cnt = int(stats["cnt"] or 0)
        conn.execute("UPDATE marketplace_profiles SET average_rating = ?, review_count = ? WHERE id = ?", (avg_r, cnt, provider_id))
        conn.commit()

# --- Favorites ---

def add_favorite(conn, user_id: int, service_id: int):
    conn.execute("INSERT INTO marketplace_favorites (user_id, service_id, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                 (user_id, service_id, _now_iso()))
    conn.commit()

def remove_favorite(conn, user_id: int, service_id: int):
    conn.execute("DELETE FROM marketplace_favorites WHERE user_id = ? AND service_id = ?", (user_id, service_id))
    conn.commit()

def is_favorite(conn, user_id: int, service_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM marketplace_favorites WHERE user_id = ? AND service_id = ?", (user_id, service_id)).fetchone()
    return bool(row)

def list_user_favorites(conn, user_id: int) -> list:
    rows = conn.execute("""
        SELECT f.created_at AS favorited_at, s.*, c.name AS category_name, p.display_name AS provider_name
        FROM marketplace_favorites f
        JOIN marketplace_services s ON s.id = f.service_id
        JOIN marketplace_categories c ON c.id = s.category_id
        JOIN marketplace_profiles p ON p.id = s.provider_id
        WHERE f.user_id = ?
        ORDER BY f.created_at DESC
    """, (user_id,)).fetchall()
    return [dict(r) for r in rows]

# --- Reports / Disputes ---

def create_report(conn, reporter_id: int, reason: str, description: str, reported_user_id: int = None,
                  service_id: int = None, order_id: int = None) -> int:
    now = _now_iso()
    cur = conn.execute(
        """INSERT INTO marketplace_reports (reporter_id, reported_user_id, service_id, order_id, reason, description, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'open', ?)""",
        (reporter_id, reported_user_id, service_id, order_id, reason.strip(), description.strip(), now)
    )
    if order_id:
        update_order_status(conn, order_id, "disputed", actor_id=reporter_id)
    conn.commit()
    return cur.lastrowid

def list_reports(conn, status: str = None, limit: int = 50, offset: int = 0) -> list:
    clauses, params = [], []
    if status:
        clauses.append("r.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    rows = conn.execute(f"""
        SELECT r.*, u.username AS reporter_username
        FROM marketplace_reports r
        JOIN admin_users u ON u.id = r.reporter_id
        {where} ORDER BY r.id DESC LIMIT ? OFFSET ?
    """, params).fetchall()
    return [dict(r) for r in rows]

def resolve_report(conn, report_id: int, status: str):
    if status not in ("under_review", "resolved", "dismissed"):
        raise ValueError("Invalid status")
    conn.execute("UPDATE marketplace_reports SET status = ?, resolved_at = ? WHERE id = ?",
                 (status, _now_iso(), report_id))
    conn.commit()

# --- Notifications ---

def create_notification(conn, user_id: int, type_str: str, title: str, message: str, related_order_id: int = None) -> int:
    cur = conn.execute(
        """INSERT INTO marketplace_notifications (user_id, type, title, message, related_order_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, type_str, title, message, related_order_id, _now_iso())
    )
    conn.commit()
    return cur.lastrowid

def list_user_notifications(conn, user_id: int, unread_only: bool = False, limit: int = 50) -> list:
    sql = "SELECT * FROM marketplace_notifications WHERE user_id = ?"
    params = [user_id]
    if unread_only:
        sql += " AND read_at IS NULL"
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]

def mark_notification_read(conn, notif_id: int, user_id: int):
    conn.execute("UPDATE marketplace_notifications SET read_at = ? WHERE id = ? AND user_id = ?",
                 (_now_iso(), notif_id, user_id))
    conn.commit()

# --- Payouts ---

def create_payout(conn, provider_id: int, order_id: int, amount: float, currency: str = "USD") -> int:
    cur = conn.execute(
        """INSERT INTO marketplace_provider_payouts (provider_id, order_id, amount, currency, status, created_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        (provider_id, order_id, float(amount), currency.upper(), _now_iso())
    )
    conn.commit()
    return cur.lastrowid

def list_payouts(conn, provider_id: int = None, status: str = None) -> list:
    clauses, params = [], []
    if provider_id:
        clauses.append("p.provider_id = ?")
        params.append(provider_id)
    if status:
        clauses.append("p.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"""
        SELECT p.*, pr.display_name AS provider_name, o.order_number
        FROM marketplace_provider_payouts p
        JOIN marketplace_profiles pr ON pr.id = p.provider_id
        JOIN marketplace_orders o ON o.id = p.order_id
        {where} ORDER BY p.id DESC
    """, params).fetchall()
    return [dict(r) for r in rows]

def mark_payout_paid(conn, payout_id: int, reference: str):
    conn.execute("UPDATE marketplace_provider_payouts SET status = 'paid', payout_reference = ?, paid_at = ? WHERE id = ?",
                 (reference, _now_iso(), payout_id))
    conn.commit()

# --- RemoteBridge Session Integration ---

def create_marketplace_session_token(conn, order_id: int, customer_id: int, provider_id: int, device_id: str,
                                      allowed_capabilities: list = None, expires_in_seconds: int = 900) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.fromtimestamp(time.time() + expires_in_seconds, tz=timezone.utc).isoformat()
    caps = json.dumps(allowed_capabilities or ["remote_control", "file_transfer", "chat"])
    conn.execute(
        """INSERT INTO marketplace_session_tokens
           (token, order_id, customer_id, provider_id, device_id, allowed_capabilities, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (token, order_id, customer_id, provider_id, device_id, caps, expires_at, _now_iso())
    )
    conn.commit()
    return token

def verify_and_burn_session_token(conn, token: str) -> dict:
    row = conn.execute("SELECT * FROM marketplace_session_tokens WHERE token = ?", (token,)).fetchone()
    if not row:
        return None
    d = dict(row)
    now_iso = _now_iso()
    if d["used_at"] is not None or d["expires_at"] < now_iso:
        return None  # Token already burned or expired
    conn.execute("UPDATE marketplace_session_tokens SET used_at = ? WHERE token = ?", (now_iso, token))
    conn.commit()
    d["allowed_capabilities"] = json.loads(d["allowed_capabilities"])
    return d

# --- Initial Seed Categories ---

def seed_default_categories(conn):
    defaults = [
        ("Remote Desktop Support", "remote-desktop-support", "Instant remote troubleshooting & technical assistance", "monitor"),
        ("Server & Cloud Admin", "server-cloud-admin", "Server maintenance, Linux/Windows & cloud setup", "server"),
        ("Network & VPN Setup", "network-vpn-setup", "Router, firewall, VPN & network infrastructure support", "globe"),
        ("Security & Antivirus Audit", "security-audit", "System hardening, malware removal & security audits", "shield"),
        ("Software Config & Install", "software-config", "Application installation, configuration & performance tuning", "tool"),
        ("Data Backup & Recovery", "data-backup-recovery", "Disaster recovery, automated backups & data restoration", "database"),
    ]
    for name, slug, desc, icon in defaults:
        if get_category_by_slug(conn, slug) is None:
            create_category(conn, name, slug, desc, icon)
