import http.client
import os
import shutil
import ssl
import subprocess
import time
import unittest

from redh_api import tls
from redh_api.config import load
from redh_api.server import App
from redh_api.shutdown import Shutdown

from support import DIGEST, Lab, Running

OPENSSL = shutil.which("openssl")


@unittest.skipUnless(OPENSSL, "openssl isn't installed")
class CertificateTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        self.certificate = self.lab.path("tls.crt")
        self.key = self.lab.path("tls.key")
        tls.make(self.certificate, self.key, "lab")

    def openssl(self, *args):
        return subprocess.run([OPENSSL, "x509", "-in", self.certificate, "-noout", *args],
                              capture_output=True, text=True, check=True).stdout

    def test_a_key_for_its_owner_and_group(self):
        self.assertEqual(os.stat(self.key).st_mode & 0o777, 0o640)
        self.assertEqual(os.stat(self.certificate).st_mode & 0o777, 0o644)

    def test_for_the_name_and_ten_years(self):
        self.assertIn("CN=lab", self.openssl("-subject").replace(" ", ""))
        until = ssl.cert_time_to_seconds(self.openssl("-enddate").strip().split("=", 1)[1])
        self.assertAlmostEqual(until, time.time() + tls.DAYS * 86400, delta=86400)

    def test_the_fingerprint_is_openssls(self):
        self.assertIn(tls.fingerprint(self.certificate), self.openssl("-fingerprint", "-sha256"))

    def test_an_existing_certificate_stays(self):
        with self.assertRaises(tls.CertificateError) as caught:
            tls.make(self.certificate, self.lab.path("other.key"), "lab")
        self.assertIn("exists already; a new certificate would break every pairing with it", str(caught.exception))
        self.assertFalse(os.path.exists(self.lab.path("other.key")))

    def test_without_openssl_nothing_stays_behind(self):
        with self.assertRaises(tls.CertificateError) as caught:
            tls.make(self.lab.path("new.crt"), self.lab.path("new.key"), "lab", openssl=self.lab.path("no-openssl"))
        self.assertIn("can't be run", str(caught.exception))
        self.assertEqual(sorted(os.listdir(self.lab.tmp)), ["tls.crt", "tls.key"])

    def test_the_pairing_code(self):
        prefix, digest, token = tls.pairing_code("t0ken", self.certificate).split(":")
        self.assertEqual((prefix, token), ("redh1", "t0ken"))
        self.assertEqual(digest, tls.fingerprint(self.certificate).replace(":", "").lower())

    def running(self, served, trusted=None):
        config = load(self.lab.config(f"[tokens]\napp = {DIGEST}\n"))
        app = App(config, Shutdown(self.lab.path("redh-shut"), 0))
        running = Running(app, tls=tls.server_context(*served), client=trusted and tls.client_context(trusted))
        self.addCleanup(running.stop)
        return running

    def test_https_with_its_own_certificate(self):
        running = self.running((self.certificate, self.key), trusted=self.certificate)
        self.assertEqual(running.request("GET", "/health")[::2], (200, {"status": "ok"}))

    def test_a_client_that_pinned_another_certificate_refuses(self):
        other = (self.lab.path("other.crt"), self.lab.path("other.key"))
        tls.make(*other, "other")
        running = self.running(other, trusted=self.certificate)
        with self.assertRaises(ssl.SSLCertVerificationError):
            running.request("GET", "/health")

    def test_no_plain_http_beside_it(self):
        running = self.running((self.certificate, self.key))
        with self.assertRaises((http.client.HTTPException, OSError)):
            running.request("GET", "/health")


if __name__ == "__main__":
    unittest.main()
