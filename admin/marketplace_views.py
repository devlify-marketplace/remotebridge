"""
RemoteBridge Marketplace Web Controllers & View Handlers.
Renders server-side templates for Marketplace Landing, Services catalog, Provider Profile,
Order Workflow, Provider Dashboard, and Admin Marketplace Management Console.
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, g, session, abort, jsonify
import store
import marketplace_store
from marketplace_payment import PaymentGatewayFactory

marketplace_web_bp = Blueprint("marketplace_web", __name__)

def get_db():
    if "db" not in g:
        g.db = store.get_conn(store.DB_PATH)
    return g.db

def get_current_user():
    if "user_id" not in session:
        return None
    return store.get_admin_user(get_db(), session["user_id"])

def web_login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated

# Context Processor helper for marketplace templates
@marketplace_web_bp.context_processor
def inject_marketplace_globals():
    conn = get_db()
    user = get_current_user()
    prof = marketplace_store.get_provider_profile(conn, user["id"]) if user else None
    unread_notifs = len(marketplace_store.list_user_notifications(conn, user["id"], unread_only=True)) if user else 0
    categories = marketplace_store.list_categories(conn, active_only=True)
    return {
        "current_provider": prof,
        "unread_notifications_count": unread_notifs,
        "nav_categories": categories,
    }

# --- Customer Public Marketplace Routes ---

@marketplace_web_bp.route("/marketplace")
def landing():
    conn = get_db()
    categories = marketplace_store.list_categories(conn, active_only=True)
    featured_services, _ = marketplace_store.search_services(conn, active_only=True, limit=6)
    recent_services, _ = marketplace_store.search_services(conn, active_only=True, sort_by="newest", limit=6)
    popular_providers = marketplace_store.list_providers(conn, limit=4)
    return render_template("marketplace/index.html", categories=categories,
                           featured_services=featured_services, recent_services=recent_services,
                           popular_providers=popular_providers)

@marketplace_web_bp.route("/marketplace/services")
def services_catalog():
    conn = get_db()
    query = request.args.get("q", "").strip()
    cat_id = request.args.get("category_id", type=int)
    min_price = request.args.get("min_price", type=float)
    max_price = request.args.get("max_price", type=float)
    min_rating = request.args.get("min_rating", type=float)
    verified = request.args.get("verified") == "1"
    country = request.args.get("country")
    pricing_type = request.args.get("pricing_type")
    sort_by = request.args.get("sort_by", "relevance")
    page = max(1, request.args.get("page", 1, type=int))
    per_page = 12
    offset = (page - 1) * per_page

    items, total = marketplace_store.search_services(
        conn, query=query, category_id=cat_id, min_price=min_price, max_price=max_price,
        min_rating=min_rating, verified_only=verified, country=country, pricing_type=pricing_type,
        sort_by=sort_by, limit=per_page, offset=offset
    )

    categories = marketplace_store.list_categories(conn, active_only=True)
    selected_category = marketplace_store.get_category_by_id(conn, cat_id) if cat_id else None

    return render_template("marketplace/services.html", services=items, total=total, page=page,
                           per_page=per_page, total_pages=(total + per_page - 1) // per_page if total > 0 else 0,
                           categories=categories, selected_category=selected_category,
                           query=query, cat_id=cat_id, min_price=min_price, max_price=max_price,
                           min_rating=min_rating, verified=verified, country=country,
                           pricing_type=pricing_type, sort_by=sort_by)

@marketplace_web_bp.route("/marketplace/services/<slug>")
def service_detail(slug):
    conn = get_db()
    service = marketplace_store.get_service_by_slug(conn, slug)
    if not service:
        flash("Service listing not found", "error")
        return redirect(url_for("marketplace_web.services_catalog"))

    provider = marketplace_store.get_provider_profile(conn, service["provider_id"])
    reviews = marketplace_store.list_provider_reviews(conn, service["provider_id"], limit=10)
    user = get_current_user()
    user_devices = store.list_devices(conn) if user else []
    is_fav = marketplace_store.is_favorite(conn, user["id"], service["id"]) if user else False

    return render_template("marketplace/service_detail.html", service=service, provider=provider,
                           reviews=reviews, user_devices=user_devices, is_favorite=is_fav)

@marketplace_web_bp.route("/marketplace/services/<slug>/order", methods=["POST"])
@web_login_required
def submit_service_order(slug):
    conn = get_db()
    service = marketplace_store.get_service_by_slug(conn, slug)
    if not service or not service["active"]:
        flash("Service is unavailable", "error")
        return redirect(url_for("marketplace_web.services_catalog"))

    user = get_current_user()
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    if prof and prof["id"] == service["provider_id"]:
        flash("You cannot request your own service", "error")
        return redirect(url_for("marketplace_web.service_detail", slug=slug))

    description = request.form.get("description", "").strip()
    device_id = request.form.get("device_id")
    scheduled_at = request.form.get("scheduled_at")

    if not description:
        flash("Please provide details of your issue/requirements", "error")
        return redirect(url_for("marketplace_web.service_detail", slug=slug))

    order = marketplace_store.create_order(
        conn, customer_id=user["id"], provider_id=service["provider_id"], service_id=service["id"],
        title=service["title"], description=description, amount=service["price"],
        currency=service["currency"], device_id=device_id, scheduled_at=scheduled_at
    )
    flash("Service request submitted successfully!", "ok")
    return redirect(url_for("marketplace_web.order_detail", id=order["id"]))

@marketplace_web_bp.route("/marketplace/providers")
def providers_catalog():
    conn = get_db()
    search = request.args.get("q", "").strip()
    verified = request.args.get("verified") == "1"
    country = request.args.get("country")
    providers = marketplace_store.list_providers(conn, verified_only=verified, country=country, search=search, limit=20)
    return render_template("marketplace/providers.html", providers=providers, search=search, verified=verified, country=country)

@marketplace_web_bp.route("/marketplace/providers/<username>")
def provider_profile(username):
    conn = get_db()
    provider = marketplace_store.get_provider_profile_by_username(conn, username)
    if not provider:
        flash("Provider profile not found", "error")
        return redirect(url_for("marketplace_web.providers_catalog"))

    services, _ = marketplace_store.search_services(conn, provider_id=provider["id"], active_only=True)
    reviews = marketplace_store.list_provider_reviews(conn, provider["id"], limit=10)
    availability = marketplace_store.get_provider_availability(conn, provider["id"])
    return render_template("marketplace/provider_profile.html", provider=provider, services=services,
                           reviews=reviews, availability=availability)

@marketplace_web_bp.route("/marketplace/orders")
@web_login_required
def customer_orders():
    conn = get_db()
    user = get_current_user()
    status = request.args.get("status")
    orders = marketplace_store.list_orders(conn, customer_id=user["id"], status=status)
    return render_template("marketplace/orders.html", orders=orders, selected_status=status)

@marketplace_web_bp.route("/marketplace/orders/<id>")
@web_login_required
def order_detail(id):
    conn = get_db()
    user = get_current_user()
    order = marketplace_store.get_order(conn, id)
    if not order:
        flash("Order not found", "error")
        return redirect(url_for("marketplace_web.customer_orders"))

    prof = marketplace_store.get_provider_profile(conn, user["id"])
    provider_id = prof["id"] if prof else None

    if order["customer_id"] != user["id"] and order["provider_id"] != provider_id and user["role"] != "admin":
        abort(403)

    messages = marketplace_store.list_order_messages(conn, order["id"])
    return render_template("marketplace/order_detail.html", order=order, messages=messages,
                           is_customer=(user["id"] == order["customer_id"]),
                           is_provider=(provider_id == order["provider_id"]))

@marketplace_web_bp.route("/marketplace/orders/<int:id>/pay", methods=["POST"])
@web_login_required
def pay_order(id):
    conn = get_db()
    user = get_current_user()
    order = marketplace_store.get_order(conn, id)
    if not order or order["customer_id"] != user["id"]:
        abort(403)

    payment_gw = PaymentGatewayFactory.get_provider("mock")
    pay_res = payment_gw.create_payment(order_id=order["id"], amount=order["amount"], currency=order["currency"])
    payment_gw.verify_payment(pay_res["payment_id"])
    marketplace_store.update_order_payment_status(conn, order["id"], "paid", reference=pay_res["reference"])

    flash("Payment confirmed! Your order is secured.", "ok")
    return redirect(url_for("marketplace_web.order_detail", id=id))

@marketplace_web_bp.route("/marketplace/orders/<int:id>/complete", methods=["POST"])
@web_login_required
def complete_order(id):
    conn = get_db()
    user = get_current_user()
    order = marketplace_store.get_order(conn, id)
    if not order:
        abort(404)
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    provider_id = prof["id"] if prof else None
    if order["customer_id"] != user["id"] and order["provider_id"] != provider_id and user["role"] != "admin":
        abort(403)

    marketplace_store.update_order_status(conn, id, "completed", actor_id=user["id"])
    flash("Order marked as completed.", "ok")
    return redirect(url_for("marketplace_web.order_detail", id=id))

@marketplace_web_bp.route("/marketplace/orders/<int:id>/review", methods=["POST"])
@web_login_required
def submit_order_review(id):
    conn = get_db()
    user = get_current_user()
    rating = request.form.get("rating", type=int)
    review_text = request.form.get("review", "").strip()

    try:
        marketplace_store.create_review(conn, id, user["id"], rating, review_text)
        flash("Thank you! Review submitted successfully.", "ok")
    except Exception as e:
        flash(f"Review error: {str(e)}", "error")
    return redirect(url_for("marketplace_web.order_detail", id=id))

@marketplace_web_bp.route("/marketplace/orders/<int:id>/dispute", methods=["POST"])
@web_login_required
def submit_order_dispute(id):
    conn = get_db()
    user = get_current_user()
    reason = request.form.get("reason", "").strip()
    description = request.form.get("description", "").strip()

    try:
        marketplace_store.create_report(conn, reporter_id=user["id"], reason=reason, description=description, order_id=id)
        flash("Dispute opened. Admin support will review shortly.", "ok")
    except Exception as e:
        flash(f"Dispute error: {str(e)}", "error")
    return redirect(url_for("marketplace_web.order_detail", id=id))

@marketplace_web_bp.route("/marketplace/favorites")
@web_login_required
def user_favorites():
    conn = get_db()
    user = get_current_user()
    favorites = marketplace_store.list_user_favorites(conn, user["id"])
    return render_template("marketplace/favorites.html", favorites=favorites)

@marketplace_web_bp.route("/marketplace/favorites/<int:service_id>/toggle", methods=["POST"])
@web_login_required
def toggle_favorite(service_id):
    conn = get_db()
    user = get_current_user()
    if marketplace_store.is_favorite(conn, user["id"], service_id):
        marketplace_store.remove_favorite(conn, user["id"], service_id)
        is_fav = False
    else:
        marketplace_store.add_favorite(conn, user["id"], service_id)
        is_fav = True

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"favorite": is_fav})
    return redirect(request.referrer or url_for("marketplace_web.user_favorites"))

# --- Provider Dashboard Routes ---

@marketplace_web_bp.route("/marketplace/provider")
@web_login_required
def provider_dashboard():
    conn = get_db()
    user = get_current_user()
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    if not prof:
        return render_template("marketplace/provider_onboarding.html")

    services, _ = marketplace_store.search_services(conn, provider_id=prof["id"], active_only=False)
    pending_requests = marketplace_store.list_orders(conn, provider_id=prof["id"], status="pending")
    active_jobs = marketplace_store.list_orders(conn, provider_id=prof["id"], status="accepted") + marketplace_store.list_orders(conn, provider_id=prof["id"], status="in_progress")
    completed_jobs = marketplace_store.list_orders(conn, provider_id=prof["id"], status="completed")

    payouts = marketplace_store.list_payouts(conn, provider_id=prof["id"])
    total_earnings = sum(p["amount"] for p in payouts)

    return render_template("marketplace/provider_dashboard.html", provider=prof, services=services,
                           pending_requests=pending_requests, active_jobs=active_jobs,
                           completed_jobs=completed_jobs, total_earnings=total_earnings)

@marketplace_web_bp.route("/marketplace/provider/profile", methods=["GET", "POST"])
@web_login_required
def edit_provider_profile():
    conn = get_db()
    user = get_current_user()
    prof = marketplace_store.get_provider_profile(conn, user["id"])

    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        headline = request.form.get("headline", "").strip()
        bio = request.form.get("bio", "").strip()
        country = request.form.get("country", "").strip()
        city = request.form.get("city", "").strip()
        langs = [l.strip() for l in request.form.get("languages", "").split(",") if l.strip()]
        skills = [s.strip() for s in request.form.get("skills", "").split(",") if s.strip()]

        if not prof:
            prof_id = marketplace_store.create_provider_profile(
                conn, user["id"], display_name=display_name or user["username"], username=user["username"],
                headline=headline, bio=bio, country=country, city=city, languages=langs, skills=skills
            )
            flash("Provider profile created successfully!", "ok")
        else:
            marketplace_store.update_provider_profile(
                conn, prof["id"], display_name=display_name, headline=headline, bio=bio,
                country=country, city=city, languages=langs, skills=skills
            )
            flash("Provider profile updated!", "ok")
        return redirect(url_for("marketplace_web.provider_dashboard"))

    return render_template("marketplace/provider_profile_edit.html", provider=prof)

@marketplace_web_bp.route("/marketplace/provider/services")
@web_login_required
def provider_services():
    conn = get_db()
    user = get_current_user()
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    if not prof:
        return redirect(url_for("marketplace_web.edit_provider_profile"))
    services, _ = marketplace_store.search_services(conn, provider_id=prof["id"], active_only=False)
    return render_template("marketplace/provider_services.html", services=services)

@marketplace_web_bp.route("/marketplace/provider/services/new", methods=["GET", "POST"])
@web_login_required
def new_provider_service():
    conn = get_db()
    user = get_current_user()
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    if not prof:
        flash("Please complete your provider profile first", "error")
        return redirect(url_for("marketplace_web.edit_provider_profile"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category_id = request.form.get("category_id", type=int)
        description = request.form.get("description", "").strip()
        price = request.form.get("price", type=float)
        currency = request.form.get("currency", "USD")
        pricing_type = request.form.get("pricing_type", "fixed")
        estimated_minutes = request.form.get("estimated_minutes", 60, type=int)
        requirements = request.form.get("requirements", "").strip()
        skills = [s.strip() for s in request.form.get("skills", "").split(",") if s.strip()]

        if not title or not category_id or price is None:
            flash("Title, category, and price are required", "error")
        else:
            marketplace_store.create_service(
                conn, provider_id=prof["id"], category_id=category_id, title=title,
                description=description, price=price, currency=currency, pricing_type=pricing_type,
                estimated_minutes=estimated_minutes, requirements=requirements, skills=skills
            )
            flash("Service listing created successfully!", "ok")
            return redirect(url_for("marketplace_web.provider_services"))

    categories = marketplace_store.list_categories(conn, active_only=True)
    return render_template("marketplace/service_form.html", service=None, categories=categories)

@marketplace_web_bp.route("/marketplace/provider/services/<int:service_id>/edit", methods=["GET", "POST"])
@web_login_required
def edit_provider_service(service_id):
    conn = get_db()
    user = get_current_user()
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    srv = marketplace_store.get_service(conn, service_id)
    if not srv or not prof or srv["provider_id"] != prof["id"]:
        abort(403)

    if request.method == "POST":
        marketplace_store.update_service(
            conn, service_id=service_id, title=request.form.get("title"), category_id=request.form.get("category_id", type=int),
            description=request.form.get("description"), price=request.form.get("price", type=float),
            currency=request.form.get("currency"), pricing_type=request.form.get("pricing_type"),
            estimated_minutes=request.form.get("estimated_minutes", type=int),
            requirements=request.form.get("requirements"),
            skills=[s.strip() for s in request.form.get("skills", "").split(",") if s.strip()]
        )
        flash("Service updated successfully!", "ok")
        return redirect(url_for("marketplace_web.provider_services"))

    categories = marketplace_store.list_categories(conn, active_only=True)
    return render_template("marketplace/service_form.html", service=srv, categories=categories)

@marketplace_web_bp.route("/marketplace/provider/services/<int:service_id>/toggle", methods=["POST"])
@web_login_required
def toggle_provider_service(service_id):
    conn = get_db()
    user = get_current_user()
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    srv = marketplace_store.get_service(conn, service_id)
    if not srv or not prof or srv["provider_id"] != prof["id"]:
        abort(403)

    marketplace_store.set_service_active(conn, service_id, not srv["active"])
    flash(f"Service status updated.", "ok")
    return redirect(url_for("marketplace_web.provider_services"))

@marketplace_web_bp.route("/marketplace/provider/orders")
@web_login_required
def provider_orders():
    conn = get_db()
    user = get_current_user()
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    if not prof:
        return redirect(url_for("marketplace_web.edit_provider_profile"))
    orders = marketplace_store.list_orders(conn, provider_id=prof["id"])
    return render_template("marketplace/provider_orders.html", orders=orders)

@marketplace_web_bp.route("/marketplace/provider/orders/<int:id>/accept", methods=["POST"])
@web_login_required
def provider_accept_order(id):
    conn = get_db()
    user = get_current_user()
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    order = marketplace_store.get_order(conn, id)
    if not order or not prof or order["provider_id"] != prof["id"]:
        abort(403)
    marketplace_store.update_order_status(conn, id, "accepted", actor_id=user["id"])
    flash("Order accepted!", "ok")
    return redirect(url_for("marketplace_web.order_detail", id=id))

@marketplace_web_bp.route("/marketplace/provider/orders/<int:id>/reject", methods=["POST"])
@web_login_required
def provider_reject_order(id):
    conn = get_db()
    user = get_current_user()
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    order = marketplace_store.get_order(conn, id)
    if not order or not prof or order["provider_id"] != prof["id"]:
        abort(403)
    marketplace_store.update_order_status(conn, id, "rejected", actor_id=user["id"])
    flash("Order rejected.", "ok")
    return redirect(url_for("marketplace_web.order_detail", id=id))

@marketplace_web_bp.route("/marketplace/provider/verification", methods=["GET", "POST"])
@web_login_required
def provider_verification():
    conn = get_db()
    user = get_current_user()
    prof = marketplace_store.get_provider_profile(conn, user["id"])
    if not prof:
        return redirect(url_for("marketplace_web.edit_provider_profile"))

    if request.method == "POST":
        marketplace_store.set_provider_verification(conn, prof["id"], "pending")
        flash("Verification request submitted for admin review.", "ok")
        return redirect(url_for("marketplace_web.provider_dashboard"))

    return render_template("marketplace/provider_verification.html", provider=prof)

# --- Admin Console Marketplace Management Routes ---

@marketplace_web_bp.route("/admin/marketplace")
@admin_required
def admin_marketplace_dashboard():
    conn = get_db()
    providers = marketplace_store.list_providers(conn, limit=100)
    services, total_services = marketplace_store.search_services(conn, active_only=False, limit=100)
    orders = marketplace_store.list_orders(conn, limit=100)
    payouts = marketplace_store.list_payouts(conn)
    reports = marketplace_store.list_reports(conn)
    categories = marketplace_store.list_categories(conn)
    fee_cfg = marketplace_store.get_platform_fee_config(conn)

    return render_template("marketplace/admin_marketplace.html", providers=providers,
                           services=services, total_services=total_services, orders=orders,
                           payouts=payouts, reports=reports, categories=categories, fee_config=fee_cfg)

@marketplace_web_bp.route("/admin/marketplace/categories/new", methods=["POST"])
@admin_required
def admin_create_category():
    conn = get_db()
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    icon = request.form.get("icon", "wrench").strip()
    if name:
        marketplace_store.create_category(conn, name=name, description=description, icon=icon)
        flash("Category created.", "ok")
    return redirect(url_for("marketplace_web.admin_marketplace_dashboard"))

@marketplace_web_bp.route("/admin/marketplace/providers/<int:id>/verify", methods=["POST"])
@admin_required
def admin_verify_provider(id):
    conn = get_db()
    status = request.form.get("status", "verified")
    marketplace_store.set_provider_verification(conn, id, status)
    flash(f"Provider verification status updated to '{status}'.", "ok")
    return redirect(url_for("marketplace_web.admin_marketplace_dashboard"))

@marketplace_web_bp.route("/admin/marketplace/services/<int:id>/feature", methods=["POST"])
@admin_required
def admin_feature_service(id):
    conn = get_db()
    srv = marketplace_store.get_service(conn, id)
    if srv:
        marketplace_store.set_service_featured(conn, id, not srv["featured"])
        flash("Service featured status toggled.", "ok")
    return redirect(url_for("marketplace_web.admin_marketplace_dashboard"))

@marketplace_web_bp.route("/admin/marketplace/settings", methods=["POST"])
@admin_required
def admin_update_fee_settings():
    conn = get_db()
    pct = request.form.get("percentage", 10.0, type=float)
    fixed = request.form.get("fixed_fee", 0.0, type=float)
    min_fee = request.form.get("min_fee", 0.0, type=float)
    max_fee = request.form.get("max_fee", 1000.0, type=float)
    marketplace_store.set_platform_fee_config(conn, pct, fixed, min_fee, max_fee)
    flash("Marketplace platform fee configuration saved.", "ok")
    return redirect(url_for("marketplace_web.admin_marketplace_dashboard"))
