import contextlib
import hashlib
import io
import os
import re
import shutil
import unittest

from redh_api import __version__, tls
from redh_api.cli import MARKS, apps_finding, main, system_finding, tls_finding
from redh_api.config import load
from redh_api.server import App
from redh_api.shutdown import Shutdown, current_user

from support import DIGEST, MINE, Lab, Running


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            status = main(list(argv))
        except SystemExit as e:
            status = e.code
    return status, out.getvalue(), err.getvalue()


class TokenTest(unittest.TestCase):
    def test_prints_a_token_and_its_line(self):
        status, out, _ = run("token", "redh-control")
        self.assertEqual(status, 0)
        token, line = re.findall(r"^    (.+)$", out, re.M)
        self.assertEqual(line, f"redh-control = {hashlib.sha256(token.encode()).hexdigest()}")

    def test_a_new_token_every_time(self):
        self.assertNotEqual(run("token", "app")[1], run("token", "app")[1])

    def test_checks_the_name(self):
        status, out, err = run("token", "My iPhone")
        self.assertEqual((status, out), (2, ""))
        self.assertIn("'My iPhone' is not a token name", err)

    @unittest.skipUnless(shutil.which("openssl"), "openssl isn't installed")
    def test_with_its_own_certificate_a_pairing_code_too(self):
        lab = Lab()
        self.addCleanup(lab.cleanup)
        certificate = lab.path("tls.crt")
        tls.make(certificate, lab.path("tls.key"), "lab")
        status, out, _ = run("token", "redh-control", "--certificate", certificate)
        self.assertEqual(status, 0)
        token, line, code = re.findall(r"^    (.+)$", out, re.M)
        self.assertEqual(code, tls.pairing_code(token, certificate))
        self.assertIn("Its pairing code, for Redh Control", out)
        self.assertEqual(len(re.findall(r"^    (.+)$", run("token", "app", "--certificate", lab.path("none"))[1], re.M)), 2)


@unittest.skipUnless(shutil.which("openssl"), "openssl isn't installed")
class CertificateCommandTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        self.certificate, self.key = self.lab.path("tls.crt"), self.lab.path("tls.key")

    def test_makes_it_and_says_what_goes_into_the_config(self):
        status, out, _ = run("certificate", "--certificate", self.certificate, "--key", self.key, "--name", "lab")
        self.assertEqual(status, 0)
        self.assertEqual(out.splitlines(), [
            f"Made {self.certificate} for lab, valid for ten years, and its key {self.key}.",
            "Its SHA-256 fingerprint, which the pairing codes carry:",
            f"    {tls.fingerprint(self.certificate)}",
            "",
            "For /etc/redh-api/redh-api.conf, so that RedhAPI serves HTTPS itself on every address of this server,",
            "without Caddy; restart redh-api afterwards:",
            "    [api]",
            "    listen = 0.0.0.0:8470",
            "    [tls]",
            f"    certificate = {self.certificate}",
            f"    key = {self.key}",
        ])

    def test_keeps_an_existing_one(self):
        run("certificate", "--certificate", self.certificate, "--key", self.key)
        status, _, err = run("certificate", "--certificate", self.certificate, "--key", self.key)
        self.assertEqual(status, 1)
        self.assertIn("exists already", err)

    def test_check_finds_it_and_asks_over_https(self):
        tls.make(self.certificate, self.key, "lab")
        app = App(load(self.lab.config(f"[tokens]\napp = {DIGEST}\n")), Shutdown(self.lab.path("redh-shut"), 0))
        running = Running(app, tls=tls.server_context(self.certificate, self.key))
        self.addCleanup(running.stop)
        path = self.lab.config(f"[api]\nlisten = 127.0.0.1:{running.port}\n[tls]\ncertificate = {self.certificate}\n"
                               f"key = {self.key}\n[tokens]\napp = {DIGEST}\n")
        _, out, _ = run("check", "--config", path, "--user", current_user())
        self.assertIn(f"ok  tls      its own certificate, {tls.fingerprint(self.certificate)}", out.splitlines())
        self.assertIn(f"ok  service  answers on 127.0.0.1:{running.port}", out.splitlines())

    def test_a_key_the_service_may_not_read(self):
        tls.make(self.certificate, self.key, "lab")
        os.chmod(self.key, 0)  # the tests own it: not even the owner may read it now
        self.assertEqual(tls_finding(self.certificate, self.key, current_user()),
                         (False, "tls", f"{current_user()} may not read {self.key}"))
        self.assertEqual(tls_finding(self.lab.path("none.crt"), self.key, current_user())[:2], (False, "tls"))


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)

    def test_a_broken_config(self):
        status, out, _ = run("check", "--config", self.lab.path("missing.conf"))
        self.assertEqual(status, 1)
        self.assertEqual(out, f"!!  config   {self.lab.path('missing.conf')}: No such file or directory\n")

    def test_finds_the_running_service(self):
        missing = self.lab.path("redh-shut")
        no_redh = self.lab.path("redh")
        running = Running(App(load(self.lab.config(f"[tokens]\napp = {DIGEST}\n")), Shutdown(missing, 0)))
        self.addCleanup(running.stop)
        path = self.lab.config(f"[api]\nlisten = 127.0.0.1:{running.port}\n[shutdown]\ncommand = {missing}\n"
                               f"[apps]\ncommand = {no_redh}\n[tokens]\napp = {DIGEST}\n")
        status, out, _ = run("check", "--config", path)
        self.assertEqual(status, 1)
        ok, topic, text = system_finding("redh-api")  # this machine's /sys, if it has one
        self.assertEqual(out.splitlines(), [
            f"ok  config   {path}, tokens: app",
            f"!!  command  {missing} does not exist",
            f"--  apps     not offered: {no_redh} does not exist (redh-manager isn't installed)",
            f"{MARKS[ok]}  {topic:<8} {text}",
            f"ok  service  answers on 127.0.0.1:{running.port}",
        ])

    def test_the_sensors_of_the_system_report(self):
        user = current_user()
        sys_dir = self.lab.path("sys")
        self.assertEqual(system_finding(user, sys_dir),
                         (None, "system", "no temperature sensor; no energy counter for the power"))
        self.lab.write("sys/class/hwmon/hwmon0/name", "k10temp\n")
        self.lab.write("sys/class/hwmon/hwmon0/temp1_input", "48250\n")
        self.lab.write("sys/class/hwmon/hwmon0/temp1_label", "Tctl\n")
        self.lab.write("sys/class/powercap/intel-rapl:0/name", "package-0\n")
        counter = self.lab.write("sys/class/powercap/intel-rapl:0/energy_uj", "1\n", 0o400)
        self.assertEqual(system_finding(user, sys_dir),
                         (True, "system", "temperature from k10temp: Tctl; power from package-0"))
        os.chmod(counter, 0)  # as root's is to anyone else
        self.assertEqual(system_finding(user, sys_dir),
                         (None, "system", f"temperature from k10temp: Tctl; no power: {user} may not read {counter} "
                                          "(see the README)"))
        self.assertEqual(system_finding("nobody-at-all", sys_dir),
                         (None, "system", "temperature from k10temp: Tctl; power not checked: there is no user nobody-at-all"))

    def test_apps_are_optional(self):
        user = current_user()
        redh = self.lab.write("redh", "#!/bin/sh\n", 0o755)
        self.assertEqual(apps_finding(redh, user, sudo=(self.lab.sudo(), "-n"), owners=MINE),
                         (True, "apps", f"{user} may run {redh} list --json as root"))
        self.assertEqual(apps_finding(redh, user, sudo=(self.lab.sudo(allow=False), "-n"), owners=MINE),
                         (None, "apps", f"not offered: sudo does not let {user} run {redh} list --json without a "
                                        "password (Sorry, user lab may not run sudo on lab.)"))
        os.chmod(redh, 0o777)
        self.assertEqual(apps_finding(redh, user, sudo=(self.lab.sudo(), "-n"), owners=MINE),
                         (False, "apps", f"{redh} is writable by everyone"))

    def test_nothing_answers(self):
        path = self.lab.config(f"[api]\nlisten = 127.0.0.1:9\n[tokens]\napp = {DIGEST}\n")
        status, out, _ = run("check", "--config", path)
        self.assertEqual(status, 1)
        self.assertIn("!!  service  nothing answers on 127.0.0.1:9", out)


class VersionTest(unittest.TestCase):
    def test_version(self):
        status, out, _ = run("--version")
        self.assertEqual((status, out), (0, f"redh-api {__version__}\n"))


if __name__ == "__main__":
    unittest.main()
