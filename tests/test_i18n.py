"""Phase 12 localization tests: desktop + admin catalogs, fallbacks, pseudo-locale, coverage."""
import json, os, string, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for sub in ("desktop", "admin", "tests"):
    sys.path.insert(0, os.path.join(ROOT, sub))

import i18n as desktop_i18n            # desktop/i18n.py is first on the path? make sure:
import importlib.util

def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

D = _load(os.path.join(ROOT, "desktop", "i18n.py"), "desktop_i18n_mod")
A = _load(os.path.join(ROOT, "admin", "i18n.py"), "admin_i18n_mod")
import i18n_extract_desktop as xd
import i18n_extract_admin as xa

ph = lambda t: {n for _, n, _, _ in string.Formatter().parse(t) if n}
SKIP_ADMIN = {"(see", "/api/v1/ops/", "10.0.1", "AA:BB:CC:DD:EE:FF", "cli/", "deploy/",
              "https://dl.example.com/RemoteBridgeHost-10.0.1.msi", "https://support.example.com"}


class DesktopI18n(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("REMOTEBRIDGE_LANG", "LC_ALL", "LC_MESSAGES", "LANG")}
        for k in self._env: os.environ.pop(k, None)
        D.set_language(None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is not None: os.environ[k] = v
        D.set_language(None)

    def test_default_is_english_passthrough(self):
        os.environ["LANG"] = "C"
        self.assertEqual(D.t("Disconnected"), "Disconnected")
        self.assertEqual(D.t("{viewer} left the session", viewer="bob"), "bob left the session")

    def test_spanish_and_placeholders_can_reorder(self):
        D.set_language("es")
        self.assertEqual(D.t("Disconnected"), "Desconectado")
        self.assertEqual(D.t("{viewer} left the session", viewer="bob"), "bob salió de la sesión")

    def test_detects_os_locale_and_regional_fallback(self):
        os.environ["LANG"] = "es_MX.UTF-8"
        self.assertEqual(D.current_language(), "es")
        os.environ["LANG"] = "fr_FR.UTF-8"               # no French catalog shipped
        self.assertEqual(D.current_language(), "en")
        os.environ["REMOTEBRIDGE_LANG"] = "es"; os.environ["LANG"] = "de_DE"
        self.assertEqual(D.current_language(), "es", "explicit env var beats OS locale")
        D.set_language("en"); self.assertEqual(D.current_language(), "en", "set_language beats env")

    def test_missing_translation_falls_back_to_english(self):
        D.set_language("es")
        self.assertEqual(D.t("A string nobody translated {x}", x=1), "A string nobody translated 1")

    def test_bad_translation_placeholders_never_crash(self):
        D._load("es")["Hello {name}"] = "Hola {nombre}"           # translator typo
        D.set_language("es")
        self.assertEqual(D.t("Hello {name}", name="Ana"), "Hello Ana")
        D._load("es").pop("Hello {name}")

    def test_unformattable_input_never_crashes(self):
        self.assertEqual(D.t("Policy says {oops} and {}"), "Policy says {oops} and {}")
        self.assertEqual(D.t("100% fine"), "100% fine")

    def test_pseudo_language_keeps_placeholders_and_marks_text(self):
        D.set_language("en-XA")
        out = D.t("{viewer} left the session", viewer="bob")
        self.assertTrue(out.startswith("[") and out.endswith("]"))
        self.assertIn("bob", out)
        self.assertNotEqual(out, "bob left the session")

    def test_every_translatable_string_has_a_spanish_entry_with_matching_placeholders(self):
        cat = json.load(open(os.path.join(ROOT, "desktop", "locales", "es.json"), encoding="utf-8"))["strings"]
        keys = xd.extract()
        self.assertEqual([k for k in keys if k not in cat], [])
        self.assertEqual([k for k in keys if ph(k) != ph(cat[k])], [])
        self.assertEqual([k for k in cat if k not in keys], [], "stale catalog entries")

    def test_available_languages(self):
        self.assertEqual(D.available_languages()["es"], "Español")
        self.assertEqual(D.available_languages()["en"], "English")


class AdminI18n(unittest.TestCase):
    def test_negotiate(self):
        self.assertEqual(A.negotiate("es-ES,es;q=0.9,en;q=0.8"), "es")
        self.assertEqual(A.negotiate("en-US,en;q=0.9,es;q=0.8"), "en")
        self.assertEqual(A.negotiate("fr-FR,fr;q=0.9,es;q=0.5"), "es", "falls through to next supported")
        self.assertEqual(A.negotiate("fr-FR"), "en")
        self.assertEqual(A.negotiate(""), "en")
        self.assertEqual(A.negotiate("es;q=abc, en"), "en", "garbage q-value ignored, not a crash")

    def test_translate_and_fallbacks(self):
        self.assertEqual(A.translate("Dashboard", "es"), "Panel")
        self.assertEqual(A.translate("Dashboard", "en"), "Dashboard")
        self.assertEqual(A.translate("Wake {device}", "es", device="pc1"), "Activar pc1")
        self.assertEqual(A.translate("Untranslated {x}", "es", x=2), "Untranslated 2")

    def test_catalog_complete_and_placeholders_match(self):
        cat = json.load(open(os.path.join(ROOT, "admin", "locales", "es.json"), encoding="utf-8"))["strings"]
        keys = [k for k in xa.extract() if k not in SKIP_ADMIN]
        self.assertEqual([k for k in keys if k not in cat], [])
        self.assertEqual([k for k in keys if ph(k) != ph(cat[k])], [])
        self.assertEqual([k for k in cat if k not in keys], [], "stale catalog entries")


if __name__ == "__main__":
    unittest.main(verbosity=2)
