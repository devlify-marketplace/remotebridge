"""
RemoteBridge Marketplace Versioned REST API endpoints (/api/v1/marketplace/...).
Provides REST interfaces for Flutter mobile controller, external integrations,
and web clients with full authentication, validation, fee calculation, and security controls.
"""

from flask import Blueprint, request, jsonify, g, session
import store
import marketplace_store
from marketplace_payment import PaymentGatewayFactory, verify_webhook_signature

marketplace_api_bp = Blueprint("marketplace_api", __name__, url_prefix="/api/v1/marketplace")

def get_db():
    if "db" not in g:
        g.db = store.get_conn(store.DB_PATH)
    return g.db

def _get_api_user():
    """Extract authenticated user from session or Bearer API key header."""
    conn = get_db()
    if "user_id" in session:
        return store.get_admin_user(conn, session["user_id"])

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        user_id = store.verify_api_key(conn, token)
        if user_id:
            return store.get_admin_user(conn, user_id)
    return None

def api_auth_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _get_api_user()
        if not user:
            return jsonify({"error": "Unauthorized", "message": "Authentication required"}), 401
        g.api_user = user
        return f(*args, **kwargs)
    return decorated

# --- Public Endpoints ---

@marketplace_api_bp.route("/categories", methods=["GET"])
def get_categories():
    conn = get_db()
    cats = marketplace_store.list_categories(conn, active_only=True)
    return jsonify({"categories": cats})

@marketplace_api_bp.route("/services", methods=["GET"])
def search_services():
    conn = get_db()
    query = request.args.get("q")
    cat_id = request.args.get("category_id", type=int)
    min_price = request.args.get("min_price", type=float)
    max_price = request.args.get("max_price", type=float)
    min_rating = request.args.get("min_rating", type=float)
    verified = request.args.get("verified", "") == "1"
    country = request.args.get("country")
    pricing_type = request.args.get("pricing_type")
    sort_by = request.args.get("sort_by", "relevance")
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(50, max(1, request.args.get("per_page", 20, type=int)))
    offset = (page - 1) * per_page

    items, total = marketplace_store.search_services(
        conn, query=query, category_id=cat_id, min_price=min_price, max_price=max_price,
        min_rating=min_rating, verified_only=verified, country=country, pricing_type=pricing_type,
        sort_by=sort_by, limit=per_page, offset=offset
    )
    return jsonify({
        "services": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if total > 0 else 0
    })

@marketplace_api_bp.route("/services/<id_or_slug>", methods=["GET"])
def get_service_details(id_or_slug):
    conn = get_db()
    if id_or_slug.isdigit():
        srv = marketplace_store.get_service(conn, int(id_or_slug))
    else:
        srv = marketplace_store.get_service_by_slug(conn, id_or_slug)
    if not srv:
        return jsonify({"error": "Not Found", "message": "Service listing not found"}), 404
    return jsonify({"service": srv})

@marketplace_api_bp.route("/providers/<id_or_username>", methods=["GET"])
def get_provider_details(id_or_username):
    conn = get_db()
    if id_or_username.isdigit():
        prof = marketplace_store.get_provider_profile(conn, int(id_or_username))
    else:
        prof = marketplace_store.get_provider_profile_by_username(conn, id_or_username)
    if not prof:
        return jsonify({"error": "Not Found", "message": "Provider profile not found"}), 404
    services, _ = marketplace_store.search_services(conn, provider_id=prof["id"], active_only=True)
    reviews = marketplace_store.list_provider_reviews(conn, prof["id"], limit=10)
    availability = marketplace_store.get_provider_availability(conn, prof["id"])
    return jsonify({
        "provider": prof,
        "services": services,
        "reviews": reviews,
        "availability": availability
    })

# --- Service Management (Provider) ---

@marketplace_api_bp.route("/services", methods=["POST"])
@api_auth_required
def create_service():
    conn = get_db()
    user = g.api_user
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    if not prof:
        return jsonify({"error": "Forbidden", "message": "You must create a provider profile first"}), 403

    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    category_id = data.get("category_id")
    description = (data.get("description") or "").strip()
    price = data.get("price")
    pricing_type = data.get("pricing_type", "fixed")
    estimated_minutes = data.get("estimated_minutes", 60)
    skills = data.get("skills", [])
    requirements = data.get("requirements", "")

    if not title or not category_id or not description or price is None or price < 0:
        return jsonify({"error": "Bad Request", "message": "Invalid title, category, description, or price"}), 400

    try:
        service_id = marketplace_store.create_service(
            conn, provider_id=prof["id"], category_id=int(category_id), title=title,
            description=description, price=float(price), currency=data.get("currency", "USD"),
            pricing_type=pricing_type, estimated_minutes=int(estimated_minutes),
            requirements=requirements, skills=skills
        )
        return jsonify({"message": "Service created successfully", "service": marketplace_store.get_service(conn, service_id)}), 201
    except Exception as e:
        return jsonify({"error": "Bad Request", "message": str(e)}), 400

@marketplace_api_bp.route("/services/<int:service_id>", methods=["PATCH"])
@api_auth_required
def update_service(service_id):
    conn = get_db()
    user = g.api_user
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    srv = marketplace_store.get_service(conn, service_id)
    if not srv:
        return jsonify({"error": "Not Found", "message": "Service not found"}), 404
    if srv["provider_id"] != prof["id"] and user["role"] != "admin":
        return jsonify({"error": "Forbidden", "message": "Unauthorized"}), 403

    data = request.get_json() or {}
    try:
        marketplace_store.update_service(
            conn, service_id=service_id, title=data.get("title"), category_id=data.get("category_id"),
            description=data.get("description"), price=data.get("price"), currency=data.get("currency"),
            pricing_type=data.get("pricing_type"), estimated_minutes=data.get("estimated_minutes"),
            requirements=data.get("requirements"), skills=data.get("skills")
        )
        if "active" in data:
            marketplace_store.set_service_active(conn, service_id, bool(data["active"]))
        return jsonify({"message": "Service updated successfully", "service": marketplace_store.get_service(conn, service_id)})
    except Exception as e:
        return jsonify({"error": "Bad Request", "message": str(e)}), 400

# --- Orders API ---

@marketplace_api_bp.route("/orders", methods=["POST"])
@api_auth_required
def create_order():
    conn = get_db()
    user = g.api_user
    data = request.get_json() or {}
    service_id = data.get("service_id")
    description = (data.get("description") or "").strip()
    device_id = data.get("device_id")
    scheduled_at = data.get("scheduled_at")

    if not service_id or not description:
        return jsonify({"error": "Bad Request", "message": "Service ID and problem description required"}), 400

    srv = marketplace_store.get_service(conn, int(service_id))
    if not srv or not srv["active"]:
        return jsonify({"error": "Not Found", "message": "Service listing unavailable"}), 404

    # Prevent ordering your own service
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    if prof and prof["id"] == srv["provider_id"]:
        return jsonify({"error": "Forbidden", "message": "Cannot order your own service"}), 403

    # Server-side price authority (never trust price from client)
    order_dict = marketplace_store.create_order(
        conn, customer_id=user["id"], provider_id=srv["provider_id"], service_id=srv["id"],
        title=srv["title"], description=description, amount=srv["price"], currency=srv["currency"],
        device_id=device_id, scheduled_at=scheduled_at
    )
    return jsonify({"message": "Order created successfully", "order": order_dict}), 201

@marketplace_api_bp.route("/orders", methods=["GET"])
@api_auth_required
def list_orders():
    conn = get_db()
    user = g.api_user
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    provider_id = prof["id"] if prof else None

    as_role = request.args.get("role", "customer")
    status = request.args.get("status")

    if as_role == "provider" and provider_id:
        orders = marketplace_store.list_orders(conn, provider_id=provider_id, status=status)
    else:
        orders = marketplace_store.list_orders(conn, customer_id=user["id"], status=status)
    return jsonify({"orders": orders})

@marketplace_api_bp.route("/orders/<int:order_id>", methods=["GET"])
@api_auth_required
def get_order_details(order_id):
    conn = get_db()
    user = g.api_user
    order = marketplace_store.get_order(conn, order_id)
    if not order:
        return jsonify({"error": "Not Found", "message": "Order not found"}), 404

    prof = marketplace_store.get_provider_profile(conn, user["id"])
    provider_id = prof["id"] if prof else None

    if order["customer_id"] != user["id"] and order["provider_id"] != provider_id and user["role"] != "admin":
        return jsonify({"error": "Forbidden", "message": "Unauthorized"}), 403

    messages = marketplace_store.list_order_messages(conn, order_id)
    return jsonify({"order": order, "messages": messages})

@marketplace_api_bp.route("/orders/<int:order_id>/accept", methods=["POST"])
@api_auth_required
def accept_order(order_id):
    conn = get_db()
    user = g.api_user
    order = marketplace_store.get_order(conn, order_id)
    if not order:
        return jsonify({"error": "Not Found", "message": "Order not found"}), 404
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    if not prof or order["provider_id"] != prof["id"]:
        return jsonify({"error": "Forbidden", "message": "Only the assigned provider can accept"}), 403

    updated = marketplace_store.update_order_status(conn, order_id, "accepted", actor_id=user["id"])
    return jsonify({"message": "Order accepted", "order": updated})

@marketplace_api_bp.route("/orders/<int:order_id>/reject", methods=["POST"])
@api_auth_required
def reject_order(order_id):
    conn = get_db()
    user = g.api_user
    order = marketplace_store.get_order(conn, order_id)
    if not order:
        return jsonify({"error": "Not Found", "message": "Order not found"}), 404
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    if not prof or order["provider_id"] != prof["id"]:
        return jsonify({"error": "Forbidden", "message": "Only the assigned provider can reject"}), 403

    updated = marketplace_store.update_order_status(conn, order_id, "rejected", actor_id=user["id"])
    return jsonify({"message": "Order rejected", "order": updated})

@marketplace_api_bp.route("/orders/<int:order_id>/complete", methods=["POST"])
@api_auth_required
def complete_order(order_id):
    conn = get_db()
    user = g.api_user
    order = marketplace_store.get_order(conn, order_id)
    if not order:
        return jsonify({"error": "Not Found", "message": "Order not found"}), 404

    prof = marketplace_store.get_provider_profile(conn, user["id"])
    provider_id = prof["id"] if prof else None

    if order["customer_id"] != user["id"] and order["provider_id"] != provider_id and user["role"] != "admin":
        return jsonify({"error": "Forbidden", "message": "Unauthorized"}), 403

    updated = marketplace_store.update_order_status(conn, order_id, "completed", actor_id=user["id"])
    return jsonify({"message": "Order completed successfully", "order": updated})

@marketplace_api_bp.route("/orders/<int:order_id>/cancel", methods=["POST"])
@api_auth_required
def cancel_order(order_id):
    conn = get_db()
    user = g.api_user
    order = marketplace_store.get_order(conn, order_id)
    if not order:
        return jsonify({"error": "Not Found", "message": "Order not found"}), 404

    prof = marketplace_store.get_provider_profile(conn, user["id"])
    provider_id = prof["id"] if prof else None

    if order["customer_id"] != user["id"] and order["provider_id"] != provider_id and user["role"] != "admin":
        return jsonify({"error": "Forbidden", "message": "Unauthorized"}), 403

    updated = marketplace_store.update_order_status(conn, order_id, "cancelled", actor_id=user["id"])
    return jsonify({"message": "Order cancelled", "order": updated})

@marketplace_api_bp.route("/orders/<int:order_id>/messages", methods=["POST"])
@api_auth_required
def post_message(order_id):
    conn = get_db()
    user = g.api_user
    data = request.get_json() or {}
    message_text = (data.get("message") or "").strip()
    if not message_text:
        return jsonify({"error": "Bad Request", "message": "Message text required"}), 400

    try:
        msg_id = marketplace_store.add_order_message(conn, order_id, user["id"], message_text, data.get("attachment_metadata"))
        return jsonify({"message": "Message sent", "message_id": msg_id}), 201
    except PermissionError as e:
        return jsonify({"error": "Forbidden", "message": str(e)}), 403
    except ValueError as e:
        return jsonify({"error": "Not Found", "message": str(e)}), 404

@marketplace_api_bp.route("/orders/<int:order_id>/review", methods=["POST"])
@api_auth_required
def post_review(order_id):
    conn = get_db()
    user = g.api_user
    data = request.get_json() or {}
    rating = data.get("rating")
    review_text = (data.get("review") or "").strip()

    if not rating or not review_text:
        return jsonify({"error": "Bad Request", "message": "Rating and review text required"}), 400

    try:
        rev_id = marketplace_store.create_review(conn, order_id, user["id"], int(rating), review_text)
        return jsonify({"message": "Review submitted successfully", "review_id": rev_id}), 201
    except (PermissionError, ValueError) as e:
        return jsonify({"error": "Bad Request", "message": str(e)}), 400

@marketplace_api_bp.route("/orders/<int:order_id>/dispute", methods=["POST"])
@api_auth_required
def open_dispute(order_id):
    conn = get_db()
    user = g.api_user
    data = request.get_json() or {}
    reason = (data.get("reason") or "").strip()
    description = (data.get("description") or "").strip()

    if not reason or not description:
        return jsonify({"error": "Bad Request", "message": "Reason and description required"}), 400

    order = marketplace_store.get_order(conn, order_id)
    if not order:
        return jsonify({"error": "Not Found", "message": "Order not found"}), 404

    reported_user = order["provider_user_id"] if user["id"] == order["customer_id"] else order["customer_id"]
    rep_id = marketplace_store.create_report(conn, reporter_id=user["id"], reason=reason, description=description,
                                              reported_user_id=reported_user, order_id=order_id)
    return jsonify({"message": "Dispute opened successfully", "report_id": rep_id}), 201

# --- Payments API ---

@marketplace_api_bp.route("/payments", methods=["POST"])
@api_auth_required
def initiate_payment():
    conn = get_db()
    user = g.api_user
    data = request.get_json() or {}
    order_id = data.get("order_id")
    provider_name = data.get("provider", "mock")

    order = marketplace_store.get_order(conn, order_id)
    if not order:
        return jsonify({"error": "Not Found", "message": "Order not found"}), 404
    if order["customer_id"] != user["id"]:
        return jsonify({"error": "Forbidden", "message": "Unauthorized"}), 403

    payment_gw = PaymentGatewayFactory.get_provider(provider_name)
    pay_res = payment_gw.create_payment(
        order_id=order["id"], amount=order["amount"], currency=order["currency"],
        customer_email=user["username"]
    )
    # Auto verify for mock provider
    if provider_name == "mock":
        payment_gw.verify_payment(pay_res["payment_id"])
        marketplace_store.update_order_payment_status(conn, order["id"], "paid", reference=pay_res["reference"])

    return jsonify({"payment": pay_res})

@marketplace_api_bp.route("/payments/webhook", methods=["POST"])
def payment_webhook():
    payload = request.get_data()
    sig = request.headers.get("X-Signature", "")
    secret = store.get_setting(get_db(), "webhook_secret", "default_secret")

    if not verify_webhook_signature(payload, sig, secret):
        return jsonify({"error": "Unauthorized", "message": "Invalid signature"}), 401

    data = request.get_json() or {}
    order_id = data.get("order_id")
    status = data.get("status")
    reference = data.get("reference")

    if order_id and status == "paid":
        marketplace_store.update_order_payment_status(get_db(), int(order_id), "paid", reference=reference)

    return jsonify({"status": "received"}), 200

# --- RemoteBridge Session Integration Tokens ---

@marketplace_api_bp.route("/sessions/token", methods=["POST"])
@api_auth_required
def generate_session_token():
    conn = get_db()
    user = g.api_user
    data = request.get_json() or {}
    order_id = data.get("order_id")

    order = marketplace_store.get_order(conn, order_id)
    if not order:
        return jsonify({"error": "Not Found", "message": "Order not found"}), 404

    prof = marketplace_store.get_provider_profile(conn, user["id"])
    provider_id = prof["id"] if prof else None

    if order["customer_id"] != user["id"] and order["provider_id"] != provider_id and user["role"] != "admin":
        return jsonify({"error": "Forbidden", "message": "Unauthorized"}), 403

    if order["status"] not in ("accepted", "scheduled", "in_progress"):
        return jsonify({"error": "Bad Request", "message": "Session can only be launched for accepted or active orders"}), 400

    device_id = order["device_id"] or request.args.get("device_id") or "demo-device"

    token = marketplace_store.create_marketplace_session_token(
        conn, order_id=order["id"], customer_id=order["customer_id"],
        provider_id=order["provider_id"], device_id=device_id
    )
    # Update order status to in_progress if starting session
    if order["status"] in ("accepted", "scheduled"):
        marketplace_store.update_order_status(conn, order["id"], "in_progress", actor_id=user["id"])

    return jsonify({
        "session_token": token,
        "expires_in": 900,
        "order_number": order["order_number"],
        "device_id": device_id
    })

@marketplace_api_bp.route("/sessions/verify", methods=["GET"])
def verify_session_token():
    conn = get_db()
    token = request.args.get("token")
    if not token:
        return jsonify({"error": "Bad Request", "message": "Token parameter required"}), 400

    token_data = marketplace_store.verify_and_burn_session_token(conn, token)
    if not token_data:
        return jsonify({"error": "Unauthorized", "message": "Token is invalid, expired, or already used"}), 401

    # Record RemoteBridge session event
    store.record_event(
        conn, device_id=token_data["device_id"], event="start",
        viewer_id=f"order-{token_data['order_id']}", decision="auto_accept",
        reason="marketplace_authorized_session"
    )

    return jsonify({"valid": True, "session": token_data})
