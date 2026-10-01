"""Phase 12 admin-console tests, through Flask's real request pipeline (not mocks):
CSRF, feedback API, localization, accessibility structure, contrast, XSS."""
import os, re, sys, tempfile, unittest
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin"))
import server, store

CSS = open(os.path.join(ROOT, "admin", "static", "style.css"), encoding="utf-8").read()


class Page(HTMLParser):
    """Collects what an accessibility audit needs from one rendered page."""
    VOID_SKIP = {"script", "style"}

    def __init__(self):
        super().__init__()
        self.text, self.tags, self.labels_for, self.controls = [], [], set(), []
        self.ths, self.tables, self.captions, self.lang, self.mains = [], 0, 0, None, 0
        self.skip_link = False; self._skip = 0; self._in_label = 0; self.attrs_seen = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs); self.tags.append(tag); self.attrs_seen.append((tag, a))
        if tag in self.VOID_SKIP: self._skip += 1
        if tag == "html": self.lang = a.get("lang")
        if tag == "main": self.mains += 1
        if tag == "label":
            self._in_label += 1
            if a.get("for"): self.labels_for.add(a["for"])
        if tag == "a" and a.get("class") == "skip-link": self.skip_link = True
        if tag == "table": self.tables += 1
        if tag == "caption": self.captions += 1
        if tag == "th": self.ths.append(a)
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit"):
            self.controls.append((tag, a, bool(self._in_label)))
        for k in ("placeholder", "title", "aria-label"):
            if a.get(k): self.text.append(a[k])

    def handle_endtag(self, tag):
        if tag in self.VOID_SKIP: self._skip -= 1
        if tag == "label": self._in_label -= 1

    def handle_data(self, data):
        if not self._skip and data.strip(): self.text.append(" ".join(data.split()))


def audit(html: str) -> Page:
    p = Page(); p.feed(html); return p


class AdminBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        server._DB_PATH = os.path.join(self.tmp, "admin.db")
        conn = store.init_db(server._DB_PATH)
        store.set_setting(conn, "flask_secret_key", "test-secret"); server.app.secret_key = "test-secret"
        store.set_setting(conn, "org_enrollment_key", "ORGKEY")
        store.create_admin_user(conn, "root", "pw-root", "admin")
        store.create_admin_user(conn, "eve", "pw-eve", "auditor")
        self.conn = conn
        server.app.config["TESTING"] = True
        self.c = server.app.test_client()

    def token(self, client=None):
        client = client or self.c
        html = client.get("/login").get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def login(self, user="root", pw="pw-root", client=None, headers=None):
        client = client or self.c
        r = client.post("/login", data={"username": user, "password": pw, "csrf_token": self.token(client)},
                        headers=headers or {})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:200])
        return client

    def enroll(self, device_id):
        r = self.c.post("/api/v1/enroll", json={"device_id": device_id, "enrollment_key": "ORGKEY"})
        self.assertEqual(r.status_code, 200); return r.get_json()["report_token"]

    def form_token(self, path):
        return re.search(r'name="csrf_token" value="([^"]+)"', self.c.get(path).get_data(as_text=True)).group(1)


class Csrf(AdminBase):
    def test_login_post_needs_token(self):
        r = self.c.post("/login", data={"username": "root", "password": "pw-root"})
        self.assertEqual(r.status_code, 400)

    def test_wrong_token_rejected_right_token_accepted(self):
        self.token()
        r = self.c.post("/login", data={"username": "root", "password": "pw-root", "csrf_token": "nope"})
        self.assertEqual(r.status_code, 400)
        self.login()

    def test_state_changing_routes_reject_missing_token(self):
        self.login()
        self.enroll("pc1")
        for path, data in [("/groups/new", {"name": "x"}), ("/devices/pc1/rename", {"display_name": "y"}),
                           ("/api-keys/new", {"label": "k"}), ("/deployment", {"form_id": "branding", "display_name": "Evil"})]:
            r = self.c.post(path, data=data)
            self.assertEqual(r.status_code, 400, path)
        self.assertNotEqual(store.get_branding(self.conn)["display_name"], "Evil")

    def test_token_from_one_session_is_useless_in_another(self):
        self.login(); tok = self.form_token("/groups")
        other = server.app.test_client(); self.login(client=other)
        r = other.post("/groups/new", data={"name": "g", "csrf_token": tok}); self.assertEqual(r.status_code, 400)

    def test_json_api_is_exempt_because_it_uses_bearer_tokens(self):
        tok = self.enroll("pc1")
        r = self.c.get("/api/v1/policy", headers={"Authorization": f"Bearer {tok}"}); self.assertEqual(r.status_code, 200)

    def test_logout_is_post_only(self):
        self.login()
        self.assertEqual(self.c.get("/logout").status_code, 405)
        r = self.c.post("/logout", data={"csrf_token": self.form_token("/")}); self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c.get("/").status_code, 302, "signed out")

    def test_every_post_form_in_every_page_carries_a_token(self):
        self.login(); self.enroll("pc1")
        store.create_feedback(self.conn, "pc1", "hello")
        for path in ("/", "/devices", "/groups", "/groups/new", "/sessions", "/users", "/deployment",
                     "/feedback", "/feedback/1"):
            html = self.c.get(path).get_data(as_text=True)
            forms = re.findall(r"<form\b[^>]*method=\"post\"[^>]*>(.*?)</form>", html, flags=re.S | re.I)
            for body in forms:
                self.assertIn('name="csrf_token"', body, f"{path}: a POST form without a CSRF token")


class FeedbackApi(AdminBase):
    def auth(self, tok): return {"Authorization": f"Bearer {tok}"}

    def test_submit_and_list_own(self):
        tok = self.enroll("pc1")
        r = self.c.post("/api/v1/feedback", json={"message": "It crashed", "category": "bug", "viewer_id": "bob",
                                                  "client_version": "12.0.0"}, headers=self.auth(tok))
        self.assertEqual(r.status_code, 201); tid = r.get_json()["ticket_id"]
        items = self.c.get("/api/v1/feedback", headers=self.auth(tok)).get_json()
        self.assertEqual([(i["id"], i["message"], i["status"], i["viewer_id"]) for i in items],
                         [(tid, "It crashed", "open", "bob")])

    def test_unauthenticated_and_bad_input(self):
        tok = self.enroll("pc1")
        self.assertEqual(self.c.post("/api/v1/feedback", json={"message": "x"}).status_code, 401)
        self.assertEqual(self.c.get("/api/v1/feedback").status_code, 401)
        self.assertEqual(self.c.post("/api/v1/feedback", json={"message": "   "}, headers=self.auth(tok)).status_code, 400)
        self.assertEqual(self.c.post("/api/v1/feedback", json={"message": "x" * 4001}, headers=self.auth(tok)).status_code, 400)
        r = self.c.post("/api/v1/feedback", json={"message": "ok", "category": "<script>"}, headers=self.auth(tok))
        self.assertEqual(r.status_code, 201)
        self.assertEqual(store.get_feedback(self.conn, r.get_json()["ticket_id"])["category"], "other", "unknown category coerced")

    def test_device_cannot_read_another_devices_tickets(self):
        a, b = self.enroll("pc-a"), self.enroll("pc-b")
        self.c.post("/api/v1/feedback", json={"message": "secret of A"}, headers=self.auth(a))
        self.assertEqual(self.c.get("/api/v1/feedback", headers=self.auth(b)).get_json(), [])

    def test_rate_limit_is_per_device(self):
        a, b = self.enroll("pc-a"), self.enroll("pc-b")
        for i in range(store.FEEDBACK_RATE_LIMIT):
            self.assertEqual(self.c.post("/api/v1/feedback", json={"message": f"m{i}"}, headers=self.auth(a)).status_code, 201)
        self.assertEqual(self.c.post("/api/v1/feedback", json={"message": "one more"}, headers=self.auth(a)).status_code, 429)
        self.assertEqual(self.c.post("/api/v1/feedback", json={"message": "fine"}, headers=self.auth(b)).status_code, 201)

    def test_admin_reply_reaches_the_device_and_response_omits_internal_fields(self):
        tok = self.enroll("pc1")
        tid = self.c.post("/api/v1/feedback", json={"message": "help"}, headers=self.auth(tok)).get_json()["ticket_id"]
        self.login()
        r = self.c.post(f"/feedback/{tid}/reply", data={"reply": "Try restarting", "resolve": "on",
                                                        "csrf_token": self.form_token(f"/feedback/{tid}")})
        self.assertEqual(r.status_code, 302)
        item = self.c.get("/api/v1/feedback", headers=self.auth(tok)).get_json()[0]
        self.assertEqual((item["reply"], item["status"]), ("Try restarting", "resolved"))
        self.assertNotIn("replied_by", item); self.assertNotIn("device_id", item)

    def test_auditor_can_read_but_not_reply(self):
        tok = self.enroll("pc1")
        tid = self.c.post("/api/v1/feedback", json={"message": "help"}, headers=self.auth(tok)).get_json()["ticket_id"]
        eve = self.login("eve", "pw-eve", client=server.app.test_client())
        self.assertEqual(eve.get(f"/feedback/{tid}").status_code, 200)
        t = re.search(r'name="csrf_token" value="([^"]+)"', eve.get("/feedback").get_data(as_text=True)).group(1)
        self.assertEqual(eve.post(f"/feedback/{tid}/reply", data={"reply": "x", "csrf_token": t}).status_code, 403)

    def test_ops_listing_needs_api_key(self):
        self.assertEqual(self.c.get("/api/v1/ops/feedback").status_code, 401)
        self.login()
        key = store.create_api_key(self.conn, "ci")
        anon = server.app.test_client()
        self.assertEqual(anon.get("/api/v1/ops/feedback", headers=self.auth(key)).status_code, 200)

    def test_xss_in_message_and_ids_is_escaped_everywhere(self):
        tok = self.enroll("<b>evil</b>")
        self.c.post("/api/v1/feedback", json={"message": "<script>alert(1)</script>", "viewer_id": "<img src=x onerror=1>"},
                    headers=self.auth(tok))
        self.login()
        for path in ("/feedback", "/feedback/1", "/devices", "/"):
            html = self.c.get(path, headers={"Accept-Language": "es"}).get_data(as_text=True)
            self.assertNotIn("<script>alert", html, path)
            self.assertNotIn("<img src=x", html, path)
            self.assertNotIn("<b>evil</b>", html, path)


class Localization(AdminBase):
    def test_accept_language_and_cookie_precedence(self):
        self.login()
        es = self.c.get("/", headers={"Accept-Language": "es-ES,es;q=0.9"}).get_data(as_text=True)
        self.assertIn('lang="es"', es); self.assertIn("Dispositivos inscritos", es)
        self.c.get("/set-language?lang=en&next=/")
        en = self.c.get("/", headers={"Accept-Language": "es"}).get_data(as_text=True)
        self.assertIn('lang="en"', en); self.assertIn("Enrolled devices", en)

    def test_set_language_is_not_an_open_redirect(self):
        for nxt in ("//evil.com", "https://evil.com", "/\\evil.com", "javascript:alert(1)"):
            loc = self.c.get("/set-language", query_string={"lang": "es", "next": nxt}).headers["Location"]
            self.assertTrue(loc.startswith("/") and not loc.startswith("//") and "evil" not in loc, (nxt, loc))
        ok = self.c.get("/set-language", query_string={"lang": "es", "next": "/devices"}).headers["Location"]
        self.assertEqual(ok, "/devices")

    def test_unknown_language_cookie_not_set(self):
        r = self.c.get("/set-language?lang=xx&next=/login")
        self.assertNotIn("lang=xx", r.headers.get("Set-Cookie", ""))

    def test_flash_messages_are_localized(self):
        self.login(headers={"Accept-Language": "es"})
        self.c.post("/groups/new", data={"name": "g1", "csrf_token": self.form_token("/groups/new")},
                    headers={"Accept-Language": "es"})
        self.assertIn("Grupo creado.", self.c.get("/groups", headers={"Accept-Language": "es"}).get_data(as_text=True))

    def test_bad_login_message_localized(self):
        r = self.c.post("/login", data={"username": "root", "password": "bad", "csrf_token": self.token()},
                        headers={"Accept-Language": "es"}, follow_redirects=True)
        self.assertIn("Usuario o contraseña incorrectos.", r.get_data(as_text=True))

    def test_plural_and_placeholder_forms(self):
        self.login(); tok = self.enroll("pc1")
        es = self.c.get("/groups", headers={"Accept-Language": "es"}).get_data(as_text=True)
        self.assertIn("1 dispositivo", es)
        en = self.c.get("/groups", headers={"Accept-Language": "en"}).get_data(as_text=True)
        self.assertIn("1 device", en); self.assertNotIn("1 devices", en)

    def test_pseudo_locale_shows_no_untranslated_ui_text(self):
        """Render every page in en-XA. Everything the app authors comes out as [accented...];
        anything left in plain ASCII must be DATA we seeded (ids, names, numbers), not UI text."""
        self.login(); self.enroll("front-desk")
        store.create_feedback(self.conn, "front-desk", "printer is broken", "bug", "bob", "12.0.0")
        store.reply_to_feedback(self.conn, 1, "we fixed it", "root", True)
        store.record_event(self.conn, "front-desk", "attempt", viewer_id="alice", decision="auto_unattended", address="10.0.0.5")
        store.set_release(self.conn, "10.0.1", "https://dl.example.com/x.msi", "a" * 64, "notes here")
        self.c.set_cookie("lang", "en-XA")
        data_ok = re.compile(r"^(front-desk|root|eve|alice|bob|Default|printer is broken|we fixed it|notes here|"
                             r"auto_unattended|attempt|start|end|RemoteBridge|10\.0\.1|10\.0\.0\.5|12\.0\.0|\d+|English|Español|cli/|deploy/|/api/v1/ops/|https://[\w./-]+|\{ \"admin_url\".*|"
                             r"[\W\d_]*|[0-9a-f:\- T.]+|auditor|admin|—|…)$")
        offenders = {}
        for path in ("/", "/devices", "/groups", "/groups/new", "/groups/1/edit", "/sessions", "/users",
                     "/deployment", "/feedback", "/feedback/1"):
            r = self.c.get(path); self.assertEqual(r.status_code, 200, path)
            for text in audit(r.get_data(as_text=True)).text:
                if not text.startswith("[") and not data_ok.match(text) and re.search(r"[A-Za-z]{3,}", text):
                    offenders.setdefault(text, []).append(path)
        for text in audit(server.app.test_client().get("/login", headers={"Accept-Language": "en-XA"}).get_data(as_text=True)).text:
            if not text.startswith("[") and not data_ok.match(text) and re.search(r"[A-Za-z]{3,}", text):
                offenders.setdefault(text, []).append("/login")
        self.assertEqual(offenders, {}, "visible text that bypasses translation")


class Accessibility(AdminBase):
    PAGES = ("/", "/devices", "/groups", "/groups/new", "/groups/1/edit", "/sessions", "/users",
             "/deployment", "/feedback", "/feedback/1")

    def setUp(self):
        super().setUp(); self.login(); self.enroll("front-desk")
        store.create_feedback(self.conn, "front-desk", "hello"); store.record_event(self.conn, "front-desk", "start", viewer_id="a")

    def pages(self):
        for path in self.PAGES:
            yield path, audit(self.c.get(path).get_data(as_text=True))
        yield "/login", audit(server.app.test_client().get("/login").get_data(as_text=True))

    def test_landmarks_language_and_skip_link(self):
        for path, p in self.pages():
            self.assertEqual(p.lang, "en", path)
            self.assertEqual(p.mains, 1, f"{path}: exactly one <main>")
            self.assertTrue(p.skip_link, f"{path}: skip link")

    def test_every_form_control_has_an_accessible_name(self):
        for path, p in self.pages():
            for tag, a, in_label in p.controls:
                named = (a.get("aria-label") or in_label or (a.get("id") and a["id"] in p.labels_for))
                self.assertTrue(named, f"{path}: unlabeled <{tag} {a}>")

    def test_tables_have_captions_and_scoped_headers(self):
        for path, p in self.pages():
            if p.tables:
                self.assertGreaterEqual(p.captions, p.tables, f"{path}: table without caption")
            for th in p.ths:
                self.assertEqual(th.get("scope"), "col", f"{path}: <th> without scope")

    def test_no_control_submits_on_change(self):
        """A <select> that submits as soon as the selection moves makes keyboard/screen-reader
        users change the group just by arrowing through the options (Phase 11 had this)."""
        for path, _p in self.pages():
            self.assertNotIn("onchange", self.c.get(path).get_data(as_text=True) if path != "/login" else "")

    def test_decorative_bars_hidden_from_assistive_tech(self):
        store.record_event(self.conn, "front-desk", "start", viewer_id="a")
        self.assertIn('class="bar-track" aria-hidden="true"', self.c.get("/").get_data(as_text=True))

    def test_current_page_is_exposed_in_nav(self):
        html = self.c.get("/devices").get_data(as_text=True)
        self.assertRegex(html, r'<a href="/devices" class="active" aria-current="page">')

    def test_flash_regions_announce(self):
        self.c.post("/groups/new", data={"name": "g9", "csrf_token": self.form_token("/groups/new")})
        html = self.c.get("/groups").get_data(as_text=True)
        self.assertIn('role="status"', html)
        r = self.c.post("/login", data={"username": "root", "password": "bad", "csrf_token": self.token()}, follow_redirects=True)
        self.assertIn('role="alert"', r.get_data(as_text=True))

    def test_high_contrast_theme_switch(self):
        self.assertIn('data-theme="auto"', self.c.get("/").get_data(as_text=True))
        self.c.get("/set-theme?theme=contrast&next=/")
        self.assertIn('data-theme="contrast"', self.c.get("/").get_data(as_text=True))
        self.c.get("/set-theme?theme=bogus&next=/")
        self.assertIn('data-theme="contrast"', self.c.get("/").get_data(as_text=True), "bogus value ignored")

    def test_language_and_contrast_controls_work_without_javascript(self):
        html = self.c.get("/").get_data(as_text=True)
        self.assertNotIn("<script", html)
        self.assertRegex(html, r'<form action="/set-language" method="get"')


def luminance(hexcolor):
    h = hexcolor.lstrip("#")
    r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def css_vars(block: str) -> dict:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block))


class Contrast(unittest.TestCase):
    def test_default_theme_meets_wcag_aa(self):
        v = css_vars(CSS[CSS.index(":root {"):CSS.index("}", CSS.index(":root {"))])
        for fg, bg in [("text", "bg"), ("text", "panel"), ("text-dim", "bg"), ("text-dim", "panel"),
                       ("accent-strong", "panel"), ("accent", "bg"), ("danger", "panel"), ("danger", "danger-bg"),
                       ("ok", "ok-bg"), ("ok", "panel")]:
            self.assertGreaterEqual(contrast(v[fg], v[bg]), 4.5, f"{fg} on {bg}")

    def test_high_contrast_theme_meets_wcag_aaa(self):
        block = CSS[CSS.index(':root[data-theme="contrast"] {'):]
        v = css_vars(block[:block.index("}")])
        for fg, bg in [("text", "panel"), ("text-dim", "panel"), ("accent", "panel"), ("accent-strong", "panel"),
                       ("danger", "danger-bg"), ("danger", "panel"), ("ok", "ok-bg"), ("ok", "panel")]:
            self.assertGreaterEqual(contrast(v[fg], v[bg]), 7.0, f"{fg} on {bg}")

    def test_os_preference_block_matches_explicit_theme(self):
        a = css_vars(CSS[CSS.index(':root[data-theme="contrast"] {'):].split("}")[0])
        b = css_vars(CSS[CSS.index("@media (prefers-contrast: more)"):].split("}")[0])
        self.assertEqual(a, b)

    def test_focus_and_motion_rules_present(self):
        self.assertIn(":focus-visible", CSS); self.assertIn("prefers-reduced-motion", CSS)
        self.assertIn("forced-colors", CSS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
