import errno
import socket
import unittest

from redh_api import __version__, system
from redh_api.config import load
from redh_api.server import App
from redh_api.shutdown import Shutdown
from redh_api.wake import Wake, magic_packet

from support import DIGEST, MINE, TOKEN, Lab, Running, wait_for

BOOT_ID = "0c5f3c1e-5a43-4c39-9a52-3a7d5f0e9b21"


class Unreachable(Wake):
    """A network that refuses every packet."""

    def send(self, mac):
        raise OSError(errno.ENETUNREACH, "Network is unreachable")


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        self.lab.write("proc/uptime", "12345.67 54321.00\n")
        self.lab.write("proc/sys/kernel/random/boot_id", BOOT_ID + "\n")
        # the magic packets go to this socket instead of the network
        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.receiver.close)
        self.receiver.bind(("127.0.0.1", 0))
        self.wake_port = self.receiver.getsockname()[1]
        self.config = load(self.lab.config(f"[api]\nname = lab\n[wake]\nbroadcast = 127.0.0.1\nport = {self.wake_port}\n"
                                           f"[tokens]\napp = {DIGEST}\n"))
        self.shutdown = Shutdown(self.lab.command(), 0, sudo=(self.lab.sudo(), "-n"), owners=MINE)
        self.addCleanup(lambda: wait_for(lambda: not self.shutdown.status()["pending"]))
        sampler = system.Sampler(self.lab.path("proc"), self.lab.path("sys"), sleep=lambda seconds: None)
        self.running = Running(App(self.config, self.shutdown, proc=self.lab.path("proc"), sampler=sampler))
        self.addCleanup(self.running.stop)

    def request(self, *args, **kwargs):
        return self.running.request(*args, **kwargs)

    def assertError(self, answer, status, code):
        self.assertEqual((answer[0], answer[2]["error"]["code"]), (status, code), answer[2])

    def test_health_needs_no_token(self):
        status, _, body = self.request("GET", "/health")
        self.assertEqual((status, body), (200, {"status": "ok"}))

    def test_status(self):
        status, headers, body = self.request("GET", "/v1/status", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body, {
            "name": "lab",
            "version": __version__,
            "boot_id": BOOT_ID,
            "uptime_seconds": 12345,
            "shutdown": {"ready": True, "problem": None, "pending": False, "last_error": None},
            "wake": {"broadcast": "127.0.0.1", "port": self.wake_port},
        })
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Server"], "RedhAPI")

    def test_without_a_valid_token(self):
        for authorization in (None, "Bearer wrong", f"Basic {TOKEN}", "Bearer", f"Bearer{TOKEN}"):
            with self.subTest(authorization=authorization):
                answer = self.request("GET", "/v1/status", headers={"Authorization": authorization} if authorization else {})
                self.assertError(answer, 401, "unauthorized")
                self.assertEqual(answer[1]["WWW-Authenticate"], 'Bearer realm="redh-api"')

    def test_shutdown(self):
        status, _, body = self.request("POST", "/v1/shutdown", token=TOKEN)
        self.assertEqual((status, body), (202, {"accepted": True, "delay_seconds": 0}))
        self.assertTrue(wait_for(lambda: self.lab.runs() == 1))

    def test_shutdown_takes_an_empty_object(self):
        self.assertEqual(self.request("POST", "/v1/shutdown", token=TOKEN, body=b"{}")[0], 202)

    def test_shutdown_takes_no_parameters(self):
        self.assertError(self.request("POST", "/v1/shutdown", token=TOKEN, body=b'{"now": true}'),
                         400, "unexpected_parameters")
        self.assertEqual(self.lab.runs(), 0)

    def test_shutdown_needs_a_token(self):
        self.assertError(self.request("POST", "/v1/shutdown"), 401, "unauthorized")
        self.assertEqual(self.lab.runs(), 0)

    def test_shutdown_unavailable(self):
        self.lab.sudo(allow=False)
        answer = self.request("POST", "/v1/shutdown", token=TOKEN)
        self.assertError(answer, 409, "shutdown_unavailable")
        self.assertIn("sudo does not let", answer[2]["error"]["message"])
        self.assertEqual(self.lab.runs(), 0)

    def received(self, seconds):
        """The packets that arrived within the given time."""
        self.receiver.settimeout(seconds)
        packets = []
        try:
            while True:
                packets.append(self.receiver.recv(1024))
                self.receiver.settimeout(0.05)
        except socket.timeout:
            return packets

    def test_wake(self):
        with self.assertLogs("redh_api", "INFO") as logs:
            status, _, body = self.request("POST", "/v1/wake", token=TOKEN, body=b'{"mac": "aa-bb-cc-dd-ee-0f"}')
        self.assertEqual((status, body), (200, {"sent": True, "mac": "AA:BB:CC:DD:EE:0F"}))
        self.assertEqual(self.received(5), [magic_packet(bytes.fromhex("aabbccddee0f"))])
        self.assertIn(f"wake-up of AA:BB:CC:DD:EE:0F requested with token app, sent to 127.0.0.1:{self.wake_port}",
                      "\n".join(logs.output))

    def test_wake_needs_a_mac_address(self):
        for body in (b"", b"{}", b"[]", b"nas", b'{"mac": 7}', b'{"mac": "nas"}', b'{"MAC": "AA:BB:CC:DD:EE:0F"}'):
            with self.subTest(body=body):
                self.assertError(self.request("POST", "/v1/wake", token=TOKEN, body=body), 400, "bad_request")
        self.assertEqual(self.received(0.2), [])

    def test_wake_takes_the_mac_only(self):
        answer = self.request("POST", "/v1/wake", token=TOKEN, body=b'{"mac": "AA:BB:CC:DD:EE:0F", "port": 7}')
        self.assertError(answer, 400, "unexpected_parameters")
        self.assertEqual(self.received(0.2), [])

    def test_wake_needs_a_token(self):
        self.assertError(self.request("POST", "/v1/wake", body=b'{"mac": "AA:BB:CC:DD:EE:0F"}'), 401, "unauthorized")
        self.assertEqual(self.received(0.2), [])

    def test_wake_failed(self):
        running = Running(App(self.config, self.shutdown, proc=self.lab.path("proc"), wake=Unreachable("192.0.2.255", 9)))
        self.addCleanup(running.stop)
        answer = running.request("POST", "/v1/wake", token=TOKEN, body=b'{"mac": "AA:BB:CC:DD:EE:0F"}')
        self.assertError(answer, 409, "wake_failed")
        self.assertEqual(answer[2]["error"]["message"], "The magic packet for AA:BB:CC:DD:EE:0F couldn't be sent "
                                                         "to 192.0.2.255:9: Network is unreachable")

    def test_system(self):
        status, _, body = self.request("GET", "/v1/system", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"cpu", "temperature", "power", "memory", "swap", "disks"})
        self.assertEqual([disk["path"] for disk in body["disks"]], ["/"])

    def test_system_needs_a_token(self):
        self.assertError(self.request("GET", "/v1/system"), 401, "unauthorized")

    def test_wrong_method(self):
        answer = self.request("GET", "/v1/shutdown", token=TOKEN)
        self.assertError(answer, 405, "method_not_allowed")
        self.assertEqual(answer[1]["Allow"], "POST")

    def test_unknown_path(self):
        self.assertError(self.request("GET", "/v1/reboot", token=TOKEN), 404, "not_found")

    def test_a_garbled_path(self):
        # with a Host header of its own, http.client sends the path without parsing it
        answer = self.request("GET", "http://[x/v1/status", token=TOKEN, headers={"Host": "lab"})
        self.assertError(answer, 400, "bad_request")

    def test_no_query_parameters(self):
        self.assertError(self.request("GET", "/v1/status?verbose=1", token=TOKEN), 400, "unexpected_parameters")

    def test_body_limit(self):
        self.assertError(self.request("POST", "/v1/shutdown", token=TOKEN, headers={"Content-Length": "1025"}),
                         413, "too_large")

    def test_http_server_errors_come_in_the_same_format(self):
        self.assertError(self.request("BREW", "/v1/status"), 501, "http_501")

    def test_the_log_has_client_and_token_name_but_not_the_token(self):
        with self.assertLogs("redh_api", "INFO") as logs:
            self.request("GET", "/v1/status", token=TOKEN, headers={"X-Forwarded-For": "192.0.2.7"})
            self.assertTrue(wait_for(lambda: any("/v1/status 200" in line for line in logs.output)))
        line = next(line for line in logs.output if "/v1/status 200" in line)
        self.assertIn("192.0.2.7 GET /v1/status 200", line)
        self.assertTrue(line.endswith(" token=app"), line)
        self.assertNotIn(TOKEN, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
