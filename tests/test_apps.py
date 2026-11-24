import json
import os
import unittest

from redh_api.apps import CACHE_SECONDS, Apps, AppsUnavailable
from redh_api.config import load
from redh_api.server import App
from redh_api.shutdown import Shutdown, current_user

from support import DIGEST, MINE, TOKEN, Lab, Running

# What redh-manager's 'redh list --json' writes, from its README.
LISTING = {
    "root": "/opt/podman",
    "apps": [{"name": "nextcloud", "layout": "quadlet/", "units": 3, "services": 2, "active": 2, "running": 1,
              "installed": "in sync", "caddy": 1}],
    "containers": {"running": 2, "in_no_app": [{"name": "scratch", "image": "docker.io/library/busybox:1",
                                                 "unit": None}]},
    "warnings": [],
}


class AppsLab(Lab):
    """A Lab with a redh that notes each run and then does what it is told."""

    def redh(self, then: str) -> str:
        return self.write("redh", f"#!/bin/sh\necho \"$*\" >> '{self.path('redh.ran')}'\n{then}\n", 0o755)

    def lists(self) -> str:
        return self.redh(f"cat <<'EOF'\n{json.dumps(LISTING, indent=2)}\nEOF")

    def redh_runs(self):
        return self._lines(self.path("redh.ran"))


class AppsTest(unittest.TestCase):
    def setUp(self):
        self.lab = AppsLab()
        self.addCleanup(self.lab.cleanup)
        self.now = 1000.0

    def apps(self, command, sudo=None):
        return Apps(command, sudo=(sudo or self.lab.sudo(), "-n"), owners=MINE, clock=lambda: self.now)

    def unavailable(self, apps):
        with self.assertRaises(AppsUnavailable) as caught:
            apps.listing()
        return str(caught.exception)

    def test_lists_what_redh_writes(self):
        redh = self.lab.lists()
        self.assertEqual(self.apps(redh).listing(), LISTING)
        self.assertEqual(self.lab.sudo_calls(), [f"-n {redh} list --json"])
        self.assertEqual(self.lab.redh_runs(), ["list --json"])

    def test_keeps_the_answer_for_a_while(self):
        apps = self.apps(self.lab.lists())
        apps.listing()
        self.now += CACHE_SECONDS - 1
        apps.listing()
        self.assertEqual(len(self.lab.redh_runs()), 1)
        self.now += 1
        apps.listing()
        self.assertEqual(len(self.lab.redh_runs()), 2)

    def test_without_redh(self):
        missing = self.lab.path("redh")
        self.assertEqual(self.unavailable(self.apps(missing)), f"{missing} does not exist; redh-manager isn't installed")
        self.assertEqual(self.lab.sudo_calls(), [])

    def test_a_redh_others_can_change_isnt_run(self):
        redh = self.lab.lists()
        os.chmod(redh, 0o777)
        self.assertEqual(self.unavailable(self.apps(redh)), f"{redh} is writable by everyone")
        self.assertEqual(self.lab.redh_runs(), [])

    def test_a_failing_redh(self):
        redh = self.lab.redh("echo 'redh: error: unrecognized arguments: --json' >&2\nexit 2")
        self.assertEqual(self.unavailable(self.apps(redh)),
                         f"{redh} list --json failed with exit status 2: redh: error: unrecognized arguments: --json")

    def test_sudo_refusing(self):
        refusing = self.lab.write("sudo-no", "#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n", 0o755)
        redh = self.lab.lists()
        self.assertEqual(self.unavailable(self.apps(redh, sudo=refusing)),
                         f"sudo does not let {current_user()} run {redh} list --json without a password "
                         "(sudo: a password is required)")

    def test_no_json(self):
        redh = self.lab.redh("echo 'APP        LAYOUT    UNITS  ACTIVE  RUNNING  INSTALLED  CADDY'")
        self.assertEqual(self.unavailable(self.apps(redh)), f"{redh} list --json didn't write a JSON object")

    def test_a_failure_isnt_kept(self):
        redh = self.lab.redh("exit 1")
        apps = self.apps(redh)
        self.unavailable(apps)
        self.lab.lists()
        self.assertEqual(apps.listing(), LISTING)


class AppsEndpointTest(unittest.TestCase):
    def setUp(self):
        self.lab = AppsLab()
        self.addCleanup(self.lab.cleanup)
        self.config = load(self.lab.config(f"[tokens]\napp = {DIGEST}\n"))

    def serve(self, redh):
        apps = Apps(redh, sudo=(self.lab.sudo(), "-n"), owners=MINE)
        running = Running(App(self.config, Shutdown(self.lab.path("redh-shut"), 0), apps=apps))
        self.addCleanup(running.stop)
        return running

    def test_answers_with_redhs_document(self):
        status, headers, body = self.serve(self.lab.lists()).request("GET", "/v1/apps", token=TOKEN)
        self.assertEqual((status, body), (200, LISTING))
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_needs_a_token(self):
        status, _, body = self.serve(self.lab.lists()).request("GET", "/v1/apps")
        self.assertEqual((status, body["error"]["code"]), (401, "unauthorized"))
        self.assertEqual(self.lab.redh_runs(), [])

    def test_takes_get_only(self):
        status, headers, _ = self.serve(self.lab.lists()).request("POST", "/v1/apps", token=TOKEN)
        self.assertEqual((status, headers["Allow"]), (405, "GET"))

    def test_without_redh_apps_are_unavailable(self):
        missing = self.lab.path("redh")
        status, _, body = self.serve(missing).request("GET", "/v1/apps", token=TOKEN)
        self.assertEqual((status, body["error"]), (409, {
            "code": "apps_unavailable",
            "message": f"{missing} does not exist; redh-manager isn't installed",
        }))


if __name__ == "__main__":
    unittest.main()
