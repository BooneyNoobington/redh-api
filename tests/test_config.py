import os
import socket
import unittest

from redh_api.config import DEFAULT_APPS_COMMAND, DEFAULT_BROADCAST, DEFAULT_COMMAND, DEFAULT_DISKS, ConfigError, load

from support import DIGEST, Lab

EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "redh-api.conf.example")
TOKENS = f"[tokens]\napp = {DIGEST}\n"


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)

    def load(self, text):
        return load(self.lab.config(text))

    def rejected(self, text):
        with self.assertRaises(ConfigError) as caught:
            self.load(text)
        return str(caught.exception)

    def test_only_a_token_is_needed(self):
        config = self.load(TOKENS)
        self.assertEqual((config.host, config.port, config.listen), ("127.0.0.1", 8470, "127.0.0.1:8470"))
        self.assertEqual(config.name, socket.gethostname())
        self.assertEqual((config.command, config.delay), (DEFAULT_COMMAND, 2.0))
        self.assertEqual(config.apps_command, DEFAULT_APPS_COMMAND)
        self.assertEqual((config.wake_broadcast, config.wake_port), (DEFAULT_BROADCAST, 9))
        self.assertEqual(config.disks, DEFAULT_DISKS)
        self.assertEqual(config.tokens, {DIGEST: "app"})

    def test_every_key(self):
        config = self.load(f"""
            [api]
            listen = [::1]:9000
            name = lab
            [shutdown]
            command = /opt/lab/stop
            delay_seconds = 0.5
            [apps]
            command = /opt/lab/redh
            [wake]
            broadcast = 192.0.2.255
            port = 7
            [system]
            disks = /
                /srv/data
            [tokens]
            app = {DIGEST.upper()}
            watch = {"0" * 64}
            """)
        self.assertEqual((config.host, config.port, config.listen), ("::1", 9000, "[::1]:9000"))
        self.assertEqual((config.name, config.command, config.delay), ("lab", "/opt/lab/stop", 0.5))
        self.assertEqual(config.apps_command, "/opt/lab/redh")
        self.assertEqual((config.wake_broadcast, config.wake_port), ("192.0.2.255", 7))
        self.assertEqual(config.disks, ("/", "/srv/data"))
        self.assertEqual(config.tokens, {DIGEST: "app", "0" * 64: "watch"})

    def test_the_example_holds_the_defaults_and_needs_a_token(self):
        with open(EXAMPLE, encoding="utf-8") as f:
            example = f.read()
        self.assertIn("no tokens in [tokens]", self.rejected(example))
        config = self.load(example + f"app = {DIGEST}\n")
        defaults = self.load(TOKENS)
        self.assertEqual((config.listen, config.name, config.command, config.delay, config.apps_command,
                          config.wake_broadcast, config.wake_port, config.disks),
                         (defaults.listen, defaults.name, defaults.command, defaults.delay, defaults.apps_command,
                          defaults.wake_broadcast, defaults.wake_port, defaults.disks))

    def test_localhost_is_loopback_too(self):
        self.assertEqual(self.load(f"[api]\nlisten = localhost:8470\n{TOKENS}").host, "localhost")

    def test_unknown_section(self):
        self.assertIn("unknown section [server]; there are [api], [shutdown], [apps], [wake], [system], [tls] and [tokens]",
                      self.rejected(f"[server]\nname = lab\n{TOKENS}"))

    def test_unknown_key(self):
        self.assertIn("unknown key 'port' in [api]", self.rejected(f"[api]\nport = 8470\n{TOKENS}"))

    def test_no_default_section(self):
        self.assertIn("[DEFAULT] is not used", self.rejected(f"[DEFAULT]\nname = lab\n{TOKENS}"))

    def test_listen_must_be_loopback(self):
        for listen in ("0.0.0.0:8470", "192.0.2.1:8470", "[::]:8470", "rapi.lab.internal:8470"):
            with self.subTest(listen=listen):
                self.assertIn("must be a loopback address", self.rejected(f"[api]\nlisten = {listen}\n{TOKENS}"))

    def test_with_its_own_certificate_any_address(self):
        tls = "[tls]\ncertificate = /etc/redh-api/tls.crt\nkey = /etc/redh-api/tls.key\n"
        for listen, host in (("0.0.0.0:8470", "0.0.0.0"), ("[::]:8470", "::"), ("192.0.2.1:8470", "192.0.2.1")):
            with self.subTest(listen=listen):
                config = self.load(f"[api]\nlisten = {listen}\n{tls}{TOKENS}")
                self.assertEqual(config.host, host)
                self.assertEqual((config.tls_certificate, config.tls_key), ("/etc/redh-api/tls.crt", "/etc/redh-api/tls.key"))
        self.assertIn("listen in [api] must be an address of this server, such as 0.0.0.0 for all of them, "
                      "not rapi.lab.internal", self.rejected(f"[api]\nlisten = rapi.lab.internal:8470\n{tls}{TOKENS}"))

    def test_without_it_loopback_only(self):
        self.assertEqual((self.load(TOKENS).tls_certificate, self.load(TOKENS).tls_key), (None, None))
        self.assertIn("With a certificate of its own in [tls], it may listen on the network.",
                      self.rejected(f"[api]\nlisten = 0.0.0.0:8470\n{TOKENS}"))

    def test_certificate_and_key_are_absolute_paths(self):
        self.assertIn("certificate in [tls] must be an absolute path, such as /etc/redh-api/tls.crt, not 'tls.crt'",
                      self.rejected(f"[tls]\ncertificate = tls.crt\nkey = /etc/redh-api/tls.key\n{TOKENS}"))
        self.assertIn("key in [tls] must be an absolute path, such as /etc/redh-api/tls.key, not ''",
                      self.rejected(f"[tls]\ncertificate = /etc/redh-api/tls.crt\n{TOKENS}"))

    def test_listen_needs_host_and_port(self):
        for listen in ("8470", "127.0.0.1", "127.0.0.1:", ":8470", "127.0.0.1:http"):
            with self.subTest(listen=listen):
                self.assertIn("must be HOST:PORT", self.rejected(f"[api]\nlisten = {listen}\n{TOKENS}"))

    def test_port_range(self):
        for listen in ("127.0.0.1:0", "127.0.0.1:65536"):
            with self.subTest(listen=listen):
                self.assertIn("out of range", self.rejected(f"[api]\nlisten = {listen}\n{TOKENS}"))

    def test_command_is_an_absolute_path_without_arguments(self):
        for command in ("redh-shut", "/usr/local/bin/redh-shut now"):
            with self.subTest(command=command):
                self.assertIn("absolute path, without arguments",
                              self.rejected(f"[shutdown]\ncommand = {command}\n{TOKENS}"))
        for command in ("redh", "/usr/local/bin/redh list --json"):
            with self.subTest(command=command):
                self.assertIn("command in [apps] must be an absolute path, without arguments (RedhAPI adds 'list --json')",
                              self.rejected(f"[apps]\ncommand = {command}\n{TOKENS}"))

    def test_delay(self):
        for delay in ("-1", "61", "soon", "nan", "inf"):
            with self.subTest(delay=delay):
                self.assertIn("delay_seconds in [shutdown] must be a number from 0 to 60",
                              self.rejected(f"[shutdown]\ndelay_seconds = {delay}\n{TOKENS}"))

    def test_wake_broadcast_is_an_ipv4_address(self):
        for broadcast in ("", "lab.internal", "192.0.2", "192.0.2.256", "ff02::1"):
            with self.subTest(broadcast=broadcast):
                self.assertIn(f"broadcast in [wake] must be an IPv4 address, such as {DEFAULT_BROADCAST} or the "
                              f"network's own, such as 192.168.1.255, not '{broadcast}'",
                              self.rejected(f"[wake]\nbroadcast = {broadcast}\n{TOKENS}"))

    def test_wake_port(self):
        for port in ("0", "65536", "discard", "-9", ""):
            with self.subTest(port=port):
                self.assertIn(f"port in [wake] must be a number from 1 to 65535, not '{port}'",
                              self.rejected(f"[wake]\nport = {port}\n{TOKENS}"))

    def test_disks_are_absolute_paths(self):
        self.assertIn("disks in [system] are absolute paths, one per line, not 'srv/data'",
                      self.rejected(f"[system]\ndisks = /\n    srv/data\n{TOKENS}"))
        self.assertEqual(self.load(f"[system]\ndisks =\n{TOKENS}").disks, ())

    def test_token_names(self):
        self.assertIn("'my phone' in [tokens] is not a token name", self.rejected(f"[tokens]\nmy phone = {DIGEST}\n"))

    def test_token_hashes(self):
        self.assertIn("app in [tokens] is not a SHA-256", self.rejected("[tokens]\napp = 1234\n"))

    def test_the_same_hash_twice(self):
        self.assertIn("app and watch in [tokens] have the same hash",
                      self.rejected(f"[tokens]\napp = {DIGEST}\nwatch = {DIGEST}\n"))

    def test_no_tokens(self):
        self.assertIn("'redh-api token NAME' makes one", self.rejected("[api]\nname = lab\n"))

    def test_missing_file(self):
        missing = self.lab.path("missing.conf")
        with self.assertRaises(ConfigError) as caught:
            load(missing)
        self.assertEqual(str(caught.exception), f"{missing}: No such file or directory")

    def test_syntax_error_names_the_file(self):
        message = self.rejected(f"app = {DIGEST}\n")
        self.assertTrue(message.startswith(self.lab.path("redh-api.conf")), message)
        self.assertIn("no section headers", message)


if __name__ == "__main__":
    unittest.main()
