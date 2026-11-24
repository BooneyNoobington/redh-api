import os
import shutil
import subprocess
import unittest

from redh_api.cli import SERVICE_USER
from redh_api.config import DEFAULT_APPS_COMMAND, DEFAULT_COMMAND, DEFAULT_CONFIG, DEFAULT_LISTEN

DEPLOY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deploy")
# Ordinary users often lack /usr/sbin in their PATH.
VISUDO = shutil.which("visudo") or shutil.which("visudo", path="/usr/sbin")

# For a service that doesn't run as root, each of these turns NoNewPrivileges= on, and sudo stops
# working; CapabilityBoundingSet= would take root's capabilities away from redh-shut.
BREAK_SUDO = {
    "NoNewPrivileges", "SystemCallFilter", "SystemCallArchitectures", "RestrictAddressFamilies",
    "RestrictNamespaces", "PrivateDevices", "ProtectKernelTunables", "ProtectKernelModules",
    "ProtectKernelLogs", "ProtectClock", "MemoryDenyWriteExecute", "RestrictRealtime",
    "RestrictSUIDSGID", "DynamicUser", "LockPersonality", "CapabilityBoundingSet",
}

# Each of these keeps the magic packets of POST /v1/wake from leaving the server.
BREAK_WAKE = {"PrivateNetwork", "IPAddressDeny"}


def lines(name):
    """The lines of a deploy file that aren't comments."""
    with open(os.path.join(DEPLOY, name), encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]


class DeployTest(unittest.TestCase):
    """The deploy files agree with the code's defaults, and the unit leaves sudo working."""

    def test_service(self):
        service = lines("redh-api.service")
        self.assertIn(f"User={SERVICE_USER}", service)
        self.assertIn(f"ExecStart=/usr/local/bin/redh-api serve --config {DEFAULT_CONFIG}", service)

    def test_service_leaves_sudo_working(self):
        for line in lines("redh-api.service"):
            self.assertNotIn(line.split("=", 1)[0], BREAK_SUDO, line)

    def test_service_lets_wake_ups_out(self):
        for line in lines("redh-api.service"):
            self.assertNotIn(line.split("=", 1)[0], BREAK_WAKE, line)

    def test_sudoers_rule(self):
        self.assertEqual(lines("redh-api.sudoers"), [
            f'Defaults:{SERVICE_USER} secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"',
            f'{SERVICE_USER} ALL=(root) NOPASSWD: {DEFAULT_COMMAND} ""',
            f"{SERVICE_USER} ALL=(root) NOPASSWD: {DEFAULT_APPS_COMMAND} list --json",
        ])

    @unittest.skipUnless(VISUDO, "visudo isn't installed")
    def test_sudoers_file_parses(self):
        result = subprocess.run([VISUDO, "-c", "-f", os.path.join(DEPLOY, "redh-api.sudoers")],
                                stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_caddy_proxies_to_the_default_address(self):
        self.assertIn(f"reverse_proxy {DEFAULT_LISTEN}", lines("redh-api.caddy"))

    def test_sysusers_makes_the_service_user(self):
        self.assertEqual(lines("redh-api.sysusers")[0].split()[:2], ["u", SERVICE_USER])

    def test_udev_rule_gives_the_energy_counters_to_the_service_group_only(self):
        rule = lines("redh-api.rules")
        self.assertEqual(len(rule), 1)
        self.assertIn('SUBSYSTEM=="powercap", KERNEL=="intel-rapl:*"', rule[0])
        self.assertIn(f'RUN+="/usr/bin/chgrp {SERVICE_USER} /sys%p/energy_uj"', rule[0])
        self.assertIn('RUN+="/usr/bin/chmod 0440 /sys%p/energy_uj"', rule[0])


if __name__ == "__main__":
    unittest.main()
