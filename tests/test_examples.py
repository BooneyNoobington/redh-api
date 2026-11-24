"""examples/redh-shut-libvirt, run against stand-ins.

virsh, systemctl, date and sleep are small scripts that act on a pretend server in a scratch
directory. Each guest is a file there, and sleep only moves a pretend clock, so waiting for the
guests takes no time.
"""

import os
import subprocess
import unittest

from redh_api.shutdown import last_line

from support import Lab

EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "examples", "redh-shut-libvirt")
START = 1_000_000

# A guest's file holds the seconds it takes to shut down once asked. "never" ignores the
# request, and "gone" stops on its own right after the first look.
VIRSH = r"""#!/bin/sh
set -eu
[ "${LIBVIRT_DEFAULT_URI-}" = qemu:///system ] || { echo "error: not qemu:///system" >&2; exit 1; }
echo "virsh $*" >> "$LAB/log"
if [ -e "$LAB/libvirt-down" ]; then
    echo "error: failed to connect to the hypervisor" >&2
    echo "error: Failed to connect socket to '/run/libvirt/virtqemud-sock': No such file or directory" >&2
    exit 1
fi
read -r now < "$LAB/clock"
case "$1" in
list)
    for file in "$LAB"/guests/*; do
        [ -e "$file" ] || continue
        name=${file##*/}
        read -r took < "$file"
        case $took in
        never) ;;
        gone) rm "$file" ;;
        *)
            asked=never
            [ ! -e "$LAB/asked/$name" ] || read -r asked < "$LAB/asked/$name"
            if [ "$asked" != never ] && [ "$now" -ge $((asked + took)) ]; then
                rm "$file"
                continue
            fi
            ;;
        esac
        echo "$name"
    done
    echo
    ;;
shutdown)
    if [ ! -e "$LAB/guests/$2" ]; then
        echo "error: Failed to shutdown domain '$2'" >&2
        echo "error: Requested operation is not valid: domain is not running" >&2
        exit 1
    fi
    echo "$now" > "$LAB/asked/$2"
    echo "Domain '$2' is being shutdown"
    ;;
*)
    echo "error: unexpected: $*" >&2
    exit 1
    ;;
esac
"""

STAND_INS = {
    "virsh": VIRSH,
    "systemctl": '#!/bin/sh\necho "systemctl $*" >> "$LAB/log"\n',
    "date": '#!/bin/sh\n[ "$*" = +%s ] || { echo "date: unexpected: $*" >&2; exit 1; }\nread -r now < "$LAB/clock"\necho "$now"\n',
    "sleep": '#!/bin/sh\nread -r now < "$LAB/clock"\necho $((now + $1)) > "$LAB/clock"\n',
}


class LibvirtExampleTest(unittest.TestCase):
    """The example under /bin/sh, which is bash on macOS and on Arch."""

    shell = "/bin/sh"

    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        for name, text in STAND_INS.items():
            self.lab.write(os.path.join("bin", name), text, 0o755)
        os.makedirs(self.lab.path("guests"))
        os.makedirs(self.lab.path("asked"))
        self.lab.write("clock", f"{START}\n")

    def run_example(self, guests=None, libvirt_down=False):
        for name, took in (guests or {}).items():
            self.lab.write(os.path.join("guests", name), f"{took}\n")
        if libvirt_down:
            self.lab.write("libvirt-down", "")
        env = {"PATH": f"{self.lab.path('bin')}:/usr/bin:/bin", "LAB": self.lab.tmp}
        return subprocess.run([self.shell, EXAMPLE], stdin=subprocess.DEVNULL, capture_output=True,
                              encoding="utf-8", env=env, timeout=30)

    def log(self):
        try:
            with open(self.lab.path("log"), encoding="utf-8") as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def clock(self):
        with open(self.lab.path("clock"), encoding="utf-8") as f:
            return int(f.read())

    def test_powers_off_at_once_without_guests(self):
        result = self.run_example()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log(), ["virsh list --name", "systemctl poweroff"])
        self.assertEqual(self.clock(), START)

    def test_shuts_the_guests_down_first(self):
        result = self.run_example({"haos": "10", "win 11": "30"})
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log()
        self.assertEqual(log[:3], ["virsh list --name", "virsh shutdown haos", "virsh shutdown win 11"])
        self.assertEqual(set(log[3:-1]), {"virsh list --name"})
        self.assertEqual(log[-1], "systemctl poweroff")
        self.assertEqual(self.clock(), START + 30)  # until the slower guest was off
        self.assertIn("Domain 'win 11' is being shutdown", result.stdout)

    def test_guests_still_running_keep_the_server_up(self):
        # Each step of the wait starts a few stand-ins, so this is the one test that waits it out.
        result = self.run_example({"haos": "never", "nas": "5", "win 11": "never"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("virsh shutdown nas", self.log())
        self.assertNotIn("systemctl poweroff", self.log())
        self.assertEqual(last_line(result.stderr), "haos, win 11 didn't shut down within 120 seconds")
        self.assertEqual(self.clock(), START + 120)

    def test_a_guest_gone_before_it_was_asked_does_no_harm(self):
        result = self.run_example({"haos": "gone"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log(), ["virsh list --name", "virsh shutdown haos", "virsh list --name",
                                      "systemctl poweroff"])
        self.assertIn("domain is not running", result.stderr)

    def test_without_libvirt_the_server_stays_up(self):
        result = self.run_example({"haos": "10"}, libvirt_down=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.log(), ["virsh list --name"])
        self.assertEqual(last_line(result.stderr),
                         "error: Failed to connect socket to '/run/libvirt/virtqemud-sock': No such file or directory")


@unittest.skipUnless(os.path.exists("/bin/dash"), "dash isn't installed")
class LibvirtExampleDashTest(LibvirtExampleTest):
    """The same under dash, which is /bin/sh on Debian."""

    shell = "/bin/dash"


if __name__ == "__main__":
    unittest.main()
