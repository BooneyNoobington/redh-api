import socket
import unittest

from redh_api.wake import Wake, magic_packet, parse_mac, spelled

MAC = bytes.fromhex("aabbccddee0f")


class MacTest(unittest.TestCase):
    def test_spellings(self):
        for text in ("AA:BB:CC:DD:EE:0F", "aa:bb:cc:dd:ee:0f", "AA-BB-CC-DD-EE-0F"):
            with self.subTest(text=text):
                self.assertEqual(parse_mac(text), MAC)

    def test_not_a_mac_address(self):
        for text in ("", "nas", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:0F:00", "AA:BB-CC:DD:EE:0F", "AABBCCDDEE0F",
                     "AG:BB:CC:DD:EE:0F", " AA:BB:CC:DD:EE:0F", "AA:BB:CC:DD:EE:0F\n"):
            with self.subTest(text=text):
                self.assertIsNone(parse_mac(text))

    def test_spelled(self):
        self.assertEqual(spelled(MAC), "AA:BB:CC:DD:EE:0F")


class WakeTest(unittest.TestCase):
    def test_magic_packet(self):
        packet = magic_packet(MAC)
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:], MAC * 16)

    def test_send(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(("127.0.0.1", 0))
            receiver.settimeout(5)
            Wake("127.0.0.1", receiver.getsockname()[1]).send(MAC)
            self.assertEqual(receiver.recv(1024), magic_packet(MAC))

    def test_status(self):
        self.assertEqual(Wake("192.0.2.255", 7).status(), {"broadcast": "192.0.2.255", "port": 7})


if __name__ == "__main__":
    unittest.main()
