import os
import unittest

from redh_api.shutdown import SUDO_RECHECK, SUDO_RECHECK_NO, Shutdown, current_user, unsafe_path

from support import MINE, Lab, wait_for


class UnsafePathTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        self.command = self.lab.command()

    def test_safe(self):
        self.assertIsNone(unsafe_path(self.command, MINE))

    def test_missing(self):
        missing = self.lab.path("missing")
        self.assertEqual(unsafe_path(missing, MINE), f"{missing} does not exist")

    def test_not_a_file(self):
        self.assertEqual(unsafe_path(self.lab.tmp, MINE), f"{self.lab.tmp} is not a file")

    def test_not_executable(self):
        os.chmod(self.command, 0o644)
        self.assertEqual(unsafe_path(self.command, MINE), f"{self.command} is not executable")

    def test_writable_file(self):
        os.chmod(self.command, 0o775)
        self.assertIn(f"{self.command} is writable by the group ", unsafe_path(self.command, MINE))
        os.chmod(self.command, 0o757)
        self.assertEqual(unsafe_path(self.command, MINE), f"{self.command} is writable by everyone")

    def test_writable_directory_above(self):
        os.chmod(self.lab.tmp, 0o777)
        self.assertEqual(unsafe_path(self.command, MINE), f"{self.lab.tmp} is writable by everyone")

    def test_owner_other_than_root(self):
        self.assertEqual(unsafe_path(self.command), f"{self.command} belongs to {current_user()}, not root")

    def test_symbolic_link_into_an_unsafe_directory(self):
        shared = self.lab.path("shared")
        os.mkdir(shared)
        os.chmod(shared, 0o777)
        link = self.lab.path("redh-shut-link")
        os.symlink(self.lab.write("shared/redh-shut", "#!/bin/sh\n", 0o755), link)
        self.assertEqual(unsafe_path(link, MINE), f"{shared} is writable by everyone")


class ShutdownTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)

    def make(self, allow=True, status=0, message="", **options):
        shutdown = Shutdown(self.lab.command(status, message), 0, sudo=(self.lab.sudo(allow), "-n"),
                            owners=MINE, **options)
        self.addCleanup(lambda: wait_for(lambda: not shutdown.status()["pending"]))
        return shutdown

    def test_ready(self):
        self.assertEqual(self.make().status(), {"ready": True, "problem": None, "pending": False, "last_error": None})

    def test_sudo_says_no(self):
        status = self.make(allow=False).status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["problem"], f"sudo does not let {current_user()} run {self.lab.path('redh-shut')} "
                                            "without a password (Sorry, user lab may not run sudo on lab.)")

    def test_sudo_is_asked_now_and_then(self):
        now = [1000.0]
        shutdown = self.make(clock=lambda: now[0])
        shutdown.status()
        shutdown.status()
        self.assertEqual(self.lab.sudo_calls(), [f"-n -l {shutdown.command}"])
        now[0] += SUDO_RECHECK
        shutdown.status()
        self.assertEqual(len(self.lab.sudo_calls()), 2)
        shutdown.problem(fresh=True)
        self.assertEqual(len(self.lab.sudo_calls()), 3)

    def test_a_no_from_sudo_is_kept_for_less_time(self):
        now = [1000.0]
        shutdown = self.make(allow=False, clock=lambda: now[0])
        shutdown.status()
        now[0] += SUDO_RECHECK_NO - 1
        shutdown.status()
        self.assertEqual(len(self.lab.sudo_calls()), 1)
        self.lab.sudo(allow=True)
        now[0] += 1
        self.assertTrue(shutdown.status()["ready"])
        self.assertEqual(len(self.lab.sudo_calls()), 2)

    def test_asks_sudo_about_the_service_user(self):
        shutdown = self.make(user="redh-api")
        self.assertIsNone(shutdown.sudo_problem())
        self.assertEqual(self.lab.sudo_calls(), [f"-n -U redh-api -l {shutdown.command}"])

    def test_runs_the_command_once_started(self):
        shutdown = self.make()
        self.assertIsNone(shutdown.accept())
        self.assertTrue(shutdown.status()["pending"])
        self.assertEqual(self.lab.runs(), 0)
        shutdown.start()
        self.assertTrue(wait_for(lambda: not shutdown.status()["pending"]))
        self.assertEqual(self.lab.runs(), 1)
        self.assertIsNone(shutdown.status()["last_error"])
        self.assertEqual(self.lab.sudo_calls()[-1], f"-n {shutdown.command}")

    def test_a_second_request_while_pending_runs_nothing_more(self):
        shutdown = self.make()
        self.assertIsNone(shutdown.accept())
        self.assertIsNone(shutdown.accept())
        shutdown.start()
        shutdown.start()
        self.assertTrue(wait_for(lambda: not shutdown.status()["pending"]))
        self.assertEqual(self.lab.runs(), 1)

    def test_a_failure_is_reported(self):
        shutdown = self.make(status=3, message="disk busy")
        with self.assertLogs("redh_api", "WARNING") as logs:
            shutdown.accept()
            shutdown.start()
            self.assertTrue(wait_for(lambda: not shutdown.status()["pending"]))
        self.assertEqual(shutdown.status()["last_error"], f"{shutdown.command} failed with exit status 3: disk busy")
        self.assertIn("WARNING:redh_api:redh-shut: disk busy", logs.output)

    def test_not_accepted_when_not_ready(self):
        shutdown = self.make(allow=False)
        self.assertIn("sudo does not let", shutdown.accept())
        shutdown.start()
        self.assertFalse(shutdown.status()["pending"])
        self.assertEqual(self.lab.runs(), 0)

    def test_start_alone_does_nothing(self):
        shutdown = self.make()
        shutdown.start()
        self.assertFalse(shutdown.status()["pending"])
        self.assertEqual(self.lab.runs(), 0)


if __name__ == "__main__":
    unittest.main()
