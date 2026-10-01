"""Phase 12 end-to-end: the REAL host_p12.py and viewer_p12.py as subprocesses, talking through
two REAL relays (one is shut down before the viewer connects) and a REAL admin console over HTTP.
Needs Xvfb (the host captures a screen, the viewer opens a window) and `openssl`; skipped without them.
Phase 11 found that fake sockets hid a deadlock for four phases - nothing here is faked."""
import os, re, shutil, socket, subprocess, sys, tempfile, threading, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("admin", "server", "desktop"):
    sys.path.insert(0, os.path.join(ROOT, sub))

NEEDS = [c for c in ("Xvfb", "openssl") if not shutil.which(c)]


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


LOGS = []      # log files to dump when something times out


def wait_for(cond, timeout=20, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        if cond(): return True
        time.sleep(0.2)
    dump = "".join(f"\n--- {os.path.basename(p)} (tail) ---\n" + "\n".join(
        l for l in read(p).splitlines() if "Deprecat" not in l and "mss.mss" not in l)[-1800:] for p in LOGS)
    raise AssertionError(f"timed out waiting for {what}{dump}")


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f: return f.read()
    except OSError:
        return ""


@unittest.skipIf(NEEDS, f"needs {', '.join(NEEDS)}")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import relay as relay_mod, store, server
        from werkzeug.serving import make_server
        cls.tmp = tempfile.mkdtemp(prefix="p12e2e-")
        cls.procs = []

        # admin console
        server._DB_PATH = os.path.join(cls.tmp, "admin.db")
        conn = store.init_db(server._DB_PATH)
        store.set_setting(conn, "flask_secret_key", "k"); server.app.secret_key = "k"
        store.set_setting(conn, "org_enrollment_key", "ORG")
        cls.store, cls.conn = store, conn
        cls.admin_port = free_port()
        cls.admin = make_server("127.0.0.1", cls.admin_port, server.app, threaded=True)
        threading.Thread(target=cls.admin.serve_forever, daemon=True).start()

        # relays
        cls.ra = relay_mod.RelayServer("127.0.0.1", 0, quiet=True, reap_interval=0.3); cls.ra.start()
        cls.rb = relay_mod.RelayServer("127.0.0.1", 0, quiet=True, reap_interval=0.3); cls.rb.start()
        cls.relays = f"127.0.0.1:{cls.ra.port},127.0.0.1:{cls.rb.port}"

        # X display + host cert/config
        cls.display = f":{90 + os.getpid() % 9}"
        cls.procs.append(subprocess.Popen(["Xvfb", cls.display, "-screen", "0", "1024x768x24"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(1.0)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", "host_key.pem", "-out",
                        "host_cert.pem", "-days", "2", "-nodes", "-subj", "/CN=test"], cwd=cls.tmp,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import auth
        cfg = auth.load_config(os.path.join(cls.tmp, "host_config.json"))
        cfg["unattended_password_salt"], cfg["unattended_password_hash"] = auth.hash_password("pw")
        auth.save_config(cfg, os.path.join(cls.tmp, "host_config.json"))

        env = dict(os.environ, DISPLAY=cls.display, PYTHONUNBUFFERED="1", QT_LOGGING_RULES="*=false",
                   REMOTEBRIDGE_KNOWN_HOSTS=os.path.join(cls.tmp, "known_hosts.json"))
        cls.env = env
        cls.host_log = os.path.join(cls.tmp, "host.log")
        LOGS.append(cls.host_log)
        cls.host = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "desktop", "host_p12.py"), "--relay", cls.relays, "--id", "devZ",
             "--admin-url", f"http://127.0.0.1:{cls.admin_port}", "--enrollment-key", "ORG", "--lang", "es",
             "--no-update-check"],
            cwd=cls.tmp, env=env, stdin=subprocess.DEVNULL, stdout=open(cls.host_log, "w"), stderr=subprocess.STDOUT)
        cls.procs.append(cls.host)
        wait_for(lambda: cls.ra.stats()["waiting"] == 1 and cls.rb.stats()["waiting"] == 1, 25,
                 "host to register on both relays")
        cls.ra.stop()          # relay A dies before any viewer connects

    @classmethod
    def tearDownClass(cls):
        for p in reversed(cls.procs):
            p.terminate()
        for p in cls.procs:
            try: p.wait(5)
            except subprocess.TimeoutExpired: p.kill()
        cls.rb.stop(); cls.admin.shutdown(); cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def start_viewer(self, name, lang="es", extra=()):
        log = os.path.join(self.tmp, f"viewer-{name}.log")
        p = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "desktop", "viewer_p12.py"), "--relay", self.relays, "--device", "devZ",
             "--id", name, "--password", "pw", "--lang", lang, "--no-voice", *extra],
            cwd=self.tmp, env=self.env, stdin=subprocess.PIPE, stdout=open(log, "w"), stderr=subprocess.STDOUT, text=True)
        self.procs.append(p); LOGS.append(log)
        return p, log

    def say(self, proc, line):
        proc.stdin.write(line + "\n"); proc.stdin.flush()

    def test_01_failover_feedback_reply_isolation_and_second_viewer(self):
        bob, blog = self.start_viewer("bob")
        wait_for(lambda: "Conexión aprobada" in read(blog), 25, "bob to be approved")
        out = read(blog)
        self.assertIn(f"paired through relay 127.0.0.1:{self.rb.port}", out, "failed over from dead relay A to B")
        self.assertIn("Modo: control", out)

        # file a ticket from the viewer prompt, in Spanish UI
        self.say(bob, "/feedback bug la pantalla se queda negra")
        wait_for(lambda: self.store.list_feedback(self.conn), 10, "ticket in the admin DB")
        t = self.store.list_feedback(self.conn)[0]
        self.assertEqual((t["device_id"], t["viewer_id"], t["category"], t["message"]),
                         ("devZ", "bob", "bug", "la pantalla se queda negra"))
        wait_for(lambda: "Gracias: sus comentarios se enviaron (ticket n.º" in read(blog), 10, "viewer ack")

        # admin replies; bob reads it back with /replies
        self.store.reply_to_feedback(self.conn, t["id"], "Reinicie el driver de vídeo", "root", True)
        self.say(bob, "/replies")
        wait_for(lambda: "Respuesta: Reinicie el driver de vídeo" in read(blog), 10, "reply shown to bob")

        # a second viewer joins through the relay while bob is still connected (multi-user + relay)
        alice, alog = self.start_viewer("alice", lang="en")
        wait_for(lambda: "Connection approved" in read(alog), 25, "alice to be approved")
        wait_for(lambda: "alice" in read(blog) and "se unió" in read(blog), 10, "bob told that alice joined, in Spanish")

        # alice must not see bob's ticket
        self.say(alice, "/replies")
        wait_for(lambda: "You haven't sent any feedback yet." in read(alog), 10, "alice's empty list")
        self.assertNotIn("la pantalla", read(alog)); self.assertNotIn("driver", read(alog))

        # alice files her own; bob's /replies still shows only bob's
        self.say(alice, "/feedback idea dark mode")
        wait_for(lambda: len(self.store.list_feedback(self.conn)) == 2, 10, "alice's ticket")
        self.say(bob, "/replies")
        time.sleep(1.5)
        self.assertNotIn("dark mode", read(blog))

        # empty message / bad usage never reach the server
        before = len(self.store.list_feedback(self.conn))
        self.say(bob, "/feedback bug")
        wait_for(lambda: "Uso: /feedback" in read(blog), 10, "usage message")
        self.assertEqual(len(self.store.list_feedback(self.conn)), before)

        host = read(self.host_log)
        self.assertIn("Conexión aprobada", host, "host operator messages localized too")
        self.assertIn("ticket #", host)

    def test_02_direct_mode_still_works_and_old_viewer_connects(self):
        """No relay: host_p12 listening directly. Connect with the new viewer AND with the Phase 10
        viewer unchanged (the wire protocol didn't change, so old viewers must keep working)."""
        ports = [free_port() for _ in range(4)]
        hlog = os.path.join(self.tmp, "host-direct.log"); LOGS.append(hlog)
        host = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "desktop", "host_p12.py"), "--video-port", str(ports[0]),
             "--input-port", str(ports[1]), "--control-port", str(ports[2]), "--audio-port", str(ports[3]),
             "--no-update-check"],
            cwd=self.tmp, env=self.env, stdin=subprocess.DEVNULL, stdout=open(hlog, "w"), stderr=subprocess.STDOUT)
        self.procs.append(host)
        wait_for(lambda: "[audio] listening" in read(hlog), 15, "direct host listening")
        common = ["--input-port", str(ports[1]), "--control-port", str(ports[2]), "--audio-port", str(ports[3]),
                  "--password", "pw", "--no-voice"]
        for name, script, extra in (("new", "viewer_p12.py", ["--lang", "en"]), ("old", "viewer_p10.py", [])):
            log = os.path.join(self.tmp, f"viewer-direct-{name}.log"); LOGS.append(log)
            p = subprocess.Popen([sys.executable, os.path.join(ROOT, "desktop", script), f"127.0.0.1:{ports[0]}",
                                  "--id", f"direct-{name}", *common, *extra],
                                 cwd=self.tmp, env=self.env, stdin=subprocess.PIPE, stdout=open(log, "w"),
                                 stderr=subprocess.STDOUT, text=True)
            self.procs.append(p)
            wait_for(lambda: "approved" in read(log).lower(), 25, f"{name} viewer approved (direct)")
            p.terminate(); p.wait(5)

    def test_03_wrong_password_is_rejected_cleanly(self):
        log = os.path.join(self.tmp, "viewer-bad.log"); LOGS.append(log)
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "desktop", "viewer_p12.py"), "--relay", self.relays,
                              "--device", "devZ", "--id", "mallory", "--password", "WRONG", "--lang", "es", "--no-voice"],
                             cwd=self.tmp, env=self.env, stdin=subprocess.PIPE, stdout=open(log, "w"),
                             stderr=subprocess.STDOUT, text=True)
        self.procs.append(p)
        wait_for(lambda: "Conexión rechazada: contraseña de acceso desatendido incorrecta" in read(log), 25,
                 "rejection with the host's reason translated")
        self.assertEqual(p.wait(10), 0)


    def _direct_host(self, tag):
        ports = [free_port() for _ in range(4)]
        hlog = os.path.join(self.tmp, f"host-{tag}.log"); LOGS.append(hlog)
        host = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "desktop", "host_p12.py"), "--video-port", str(ports[0]),
             "--input-port", str(ports[1]), "--control-port", str(ports[2]), "--audio-port", str(ports[3]),
             "--no-update-check"],
            cwd=self.tmp, env=self.env, stdin=subprocess.DEVNULL, stdout=open(hlog, "w"), stderr=subprocess.STDOUT)
        self.procs.append(host)
        wait_for(lambda: "[audio] listening" in read(hlog), 15, "direct host listening")
        return ports, hlog

    def _viewer(self, tag, ports, *extra):
        log = os.path.join(self.tmp, f"viewer-{tag}.log"); LOGS.append(log)
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "desktop", "viewer_p12.py"), f"127.0.0.1:{ports[0]}",
                              "--input-port", str(ports[1]), "--control-port", str(ports[2]),
                              "--audio-port", str(ports[3]), "--id", f"pin-{tag}", "--password", "pw",
                              "--no-voice", "--lang", "en", *extra],
                             cwd=self.tmp, env=self.env, stdin=subprocess.PIPE, stdout=open(log, "w"),
                             stderr=subprocess.STDOUT, text=True)
        self.procs.append(p)
        return p, log

    def _host_recovered(self, hlog, aborted_so_far):
        """A viewer that refuses the certificate after the video channel is noticed by the host as soon as it
        closes (it used to sit out HANDSHAKE_TIMEOUT); wait until the host has logged giving up on it."""
        wait_for(lambda: read(hlog).count("did not complete the TLS handshakes") >= aborted_so_far, 20,
                 "host to give up on the abandoned connection")

    def test_04_certificate_pinning_end_to_end(self):
        """Real host + real viewer processes: the host prints its fingerprint, first use is remembered,
        a changed certificate and a wrong --pin are refused with exit code 3, --replace-pin recovers."""
        import json, pinning
        ports, hlog = self._direct_host("pin")
        real_fp = pinning.fingerprint_pem_file(os.path.join(self.tmp, "host_cert.pem"))
        self.assertIn(real_fp, read(hlog), "host prints its certificate fingerprint at startup")
        key = pinning.key_direct("127.0.0.1", ports[0])
        kh = self.env["REMOTEBRIDGE_KNOWN_HOSTS"]

        # 1. first use (stdin is a pipe, not a tty): trusted and remembered, with a visible notice
        p, log = self._viewer("first", ports)
        wait_for(lambda: "approved" in read(log).lower(), 25, "first connection approved")
        self.assertIn("trusting and remembering", read(log))
        p.terminate(); p.wait(5)
        self.assertEqual(json.load(open(kh, encoding="utf-8"))["hosts"][key]["fingerprint"], real_fp)

        # 2. same certificate again: silent match
        p, log = self._viewer("again", ports)
        wait_for(lambda: "approved" in read(log).lower(), 25, "second connection approved")
        self.assertNotIn("trusting and remembering", read(log))
        p.terminate(); p.wait(5)

        # 3. the remembered pin no longer matches what the host presents (as if a MITM swapped the cert)
        fake = pinning.fingerprint_der(b"some other certificate")
        json.dump({"hosts": {key: {"fingerprint": fake, "first_seen": "2026-01-01T00:00:00+00:00"}}},
                  open(kh, "w", encoding="utf-8"))
        p, log = self._viewer("changed", ports)
        self.assertEqual(p.wait(25), 3)
        out = read(log)
        self.assertIn("has changed", out); self.assertIn(fake, out); self.assertIn(real_fp, out)
        self.assertNotIn("approved", out.lower())
        self.assertEqual(json.load(open(kh, encoding="utf-8"))["hosts"][key]["fingerprint"], fake, "pin untouched")
        self._host_recovered(hlog, 1)

        # 4. an explicit wrong --pin is refused even with nothing remembered
        os.remove(kh)
        p, log = self._viewer("wrongpin", ports, "--pin", fake)
        rc = p.wait(25)
        self.assertEqual(rc, 3, read(log) + "\nHOST:\n" + read(hlog)[-1500:])
        self.assertFalse(os.path.exists(kh))
        self._host_recovered(hlog, 2)

        # 5. a malformed --pin is a usage error, not a connection attempt
        p, log = self._viewer("badpin", ports, "--pin", "nonsense")
        self.assertEqual(p.wait(25), 2)

        # 6. the right --pin connects; then --replace-pin recovers from a stale remembered pin
        p, log = self._viewer("goodpin", ports, "--pin", real_fp)
        wait_for(lambda: "approved" in read(log).lower(), 25, "--pin connection approved")
        p.terminate(); p.wait(5)
        json.dump({"hosts": {key: {"fingerprint": fake}}}, open(kh, "w", encoding="utf-8"))
        p, log = self._viewer("replace", ports, "--replace-pin")
        wait_for(lambda: "approved" in read(log).lower(), 25, "--replace-pin connection approved")
        p.terminate(); p.wait(5)
        self.assertEqual(json.load(open(kh, encoding="utf-8"))["hosts"][key]["fingerprint"], real_fp)


    # --- the direct-mode accept loop must not stall behind an abandoned viewer -------------------------------

    def _half_open_viewer(self, port, source="127.0.0.1"):
        """What a viewer that refuses the certificate (or crashes) right after the video channel looks like to
        the host: the video connection is up and TLS-complete, nothing else ever arrives. Returns the live
        connection so the caller decides whether it stays open or is closed."""
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        raw = socket.socket(); raw.settimeout(5); raw.bind((source, 0)); raw.connect(("127.0.0.1", port))
        return ctx.wrap_socket(raw, server_hostname="127.0.0.1")

    def test_05_viewer_that_leaves_after_video_does_not_stall_the_next(self):
        """Regression: a viewer that opened the video channel and left made the serial accept loop wait up to
        HANDSHAKE_TIMEOUT (10 s) for channels that never came, and the next viewer's 5 s connect timed out."""
        ports, hlog = self._direct_host("stall-leaves")
        ghost = self._half_open_viewer(ports[0])
        time.sleep(0.5)                       # the host is now waiting on this viewer's input channel
        ghost.close()                         # ...and the viewer goes away
        t0 = time.monotonic()
        p, log = self._viewer("after-leaver", ports)
        wait_for(lambda: "approved" in read(log).lower(), 8, "next viewer approved despite the abandoned one")
        self.assertLess(time.monotonic() - t0, 8)
        p.terminate(); p.wait(5)

    def test_06_viewer_that_idles_after_video_does_not_block_a_viewer_elsewhere(self):
        """A viewer that keeps its video channel open and never sends the rest must not hold anyone else up.
        (Two viewers from one address still take turns - their channels can't be told apart - so the idler
        connects from 127.0.0.2 and the real viewer from 127.0.0.1.)"""
        ports, hlog = self._direct_host("stall-idles")
        idler = self._half_open_viewer(ports[0], source="127.0.0.2")
        try:
            time.sleep(0.5)
            t0 = time.monotonic()
            p, log = self._viewer("beside-idler", ports)
            wait_for(lambda: "approved" in read(log).lower(), 8, "viewer approved while another idles mid-setup")
            self.assertLess(time.monotonic() - t0, 8)
            p.terminate(); p.wait(5)
        finally:
            idler.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
