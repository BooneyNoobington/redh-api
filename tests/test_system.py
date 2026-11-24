import os
import unittest

from redh_api import system

from support import Lab

STAT = "cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 1 0 0 1 0 0 0 0 0 0\ncpu1 1 0 0 1 0 0 0 0 0 0\nintr 12345\n"


class Clock:
    """Time for the sampler, moved on by hand or by its sleep."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class Machine:
    """A /proc and a /sys in the scratch directory, with what a small Intel server would show."""

    def __init__(self, lab):
        self.lab = lab
        self.proc = lab.path("proc")
        self.sys = lab.path("sys")
        self.ticks(busy=1000, idle=9000)
        lab.write("proc/loadavg", "0.42 0.38 0.35 1/234 5678\n")
        lab.write("proc/meminfo", "MemTotal:       16000000 kB\nMemFree:         2000000 kB\n"
                                  "MemAvailable:    9000000 kB\nSwapTotal:       4000000 kB\nSwapFree:        3500000 kB\n")
        lab.write("proc/pressure/cpu", "some avg10=1.25 avg60=0.80 avg300=0.50 total=123456\n"
                                       "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        lab.write("proc/pressure/memory", "some avg10=0.00 avg60=0.10 avg300=0.05 total=42\n"
                                          "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        lab.write("sys/class/hwmon/hwmon0/name", "acpitz\n")
        lab.write("sys/class/hwmon/hwmon0/temp1_input", "27800\n")
        lab.write("sys/class/hwmon/hwmon1/name", "coretemp\n")
        lab.write("sys/class/hwmon/hwmon1/temp1_input", "46000\n")
        lab.write("sys/class/hwmon/hwmon1/temp1_label", "Package id 0\n")
        lab.write("sys/class/hwmon/hwmon1/temp2_input", "51000\n")
        lab.write("sys/class/hwmon/hwmon1/temp2_label", "Core 0\n")
        lab.write("sys/class/thermal/thermal_zone0/type", "acpitz\n")
        lab.write("sys/class/thermal/thermal_zone0/temp", "27800\n")
        lab.write("sys/class/powercap/intel-rapl:0/name", "package-0\n")
        lab.write("sys/class/powercap/intel-rapl:0/max_energy_range_uj", "262143328850\n")
        lab.write("sys/class/powercap/intel-rapl:0:0/name", "core\n")
        lab.write("sys/class/powercap/intel-rapl:0:0/energy_uj", "5\n")
        self.energy(1_000_000)

    def ticks(self, busy, idle):
        self.lab.write("proc/stat", STAT.format(busy=busy, idle=idle))

    def energy(self, microjoules):
        self.lab.write("sys/class/powercap/intel-rapl:0/energy_uj", f"{microjoules}\n")

    def sampler(self, clock, between=None):
        """A sampler whose first sleep moves the clock and lets `between` change the machine."""

        def sleep(seconds):
            clock.sleep(seconds)
            if between:
                between()

        return system.Sampler(self.proc, self.sys, clock=clock, sleep=sleep)


class ReadingTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        self.machine = Machine(self.lab)

    def test_processor_times_and_count(self):
        self.assertEqual(system.cpu_times(self.machine.proc), (1000, 10000))
        self.assertEqual(system.cpu_count(self.machine.proc), 2)

    def test_waiting_for_io_is_idle(self):
        self.lab.write("proc/stat", "cpu  10 0 0 70 20 0 0 0 0 0\n")
        self.assertEqual(system.cpu_times(self.machine.proc), (10, 100))

    def test_load_pressure_and_memory(self):
        self.assertEqual(system.load(self.machine.proc), [0.42, 0.38, 0.35])
        self.assertEqual(system.pressure("cpu", self.machine.proc), 1.25)
        self.assertEqual(system.pressure("memory", self.machine.proc), 0.0)
        memory = system.meminfo(self.machine.proc)
        self.assertEqual((memory["MemTotal"], memory["MemAvailable"]), (16_384_000_000, 9_216_000_000))

    def test_without_pressure_stall_information(self):
        self.assertIsNone(system.pressure("io", self.machine.proc))  # the machine has no proc/pressure/io

    def test_nothing_where_there_is_no_proc(self):
        missing = self.lab.path("nowhere")
        self.assertEqual((system.cpu_times(missing), system.cpu_count(missing), system.load(missing)), (None, None, None))
        self.assertEqual(system.meminfo(missing), {})

    def test_temperature_of_the_processors_main_sensor(self):
        self.assertEqual(system.temperature(self.machine.sys), (46.0, "coretemp: Package id 0"))

    def test_else_the_hottest_sensor_of_the_processor(self):
        os.remove(self.lab.path("sys/class/hwmon/hwmon1/temp1_label"))
        self.assertEqual(system.temperature(self.machine.sys), (51.0, "coretemp: Core 0"))

    def test_else_a_thermal_zone(self):
        self.lab.write("sys/class/hwmon/hwmon1/name", "nvme\n")
        self.assertEqual(system.temperature(self.machine.sys), (27.8, "acpitz"))

    def test_else_no_temperature(self):
        self.assertEqual(system.temperature(self.lab.path("nowhere")), (None, None))

    def test_one_energy_counter_per_package(self):
        self.assertEqual([os.path.basename(z) for z in system.rapl_zones(self.machine.sys)], ["intel-rapl:0"])


class SamplerTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        self.machine = Machine(self.lab)
        self.clock = Clock()

    def test_the_first_request_compares_two_readings_a_second_apart(self):
        def a_second_later():
            self.machine.ticks(busy=1250, idle=9750)
            self.machine.energy(1_000_000 + 23_400_000)

        sampler = self.machine.sampler(self.clock, a_second_later)
        self.assertEqual(sampler.measure(), (25.0, 23.4, None))
        self.assertEqual(self.clock.slept, [1.0])

    def test_a_reading_stays_for_five_seconds(self):
        sampler = self.machine.sampler(self.clock, lambda: self.machine.ticks(busy=1100, idle=9900))
        first = sampler.measure()
        self.machine.ticks(busy=9000, idle=9900)
        self.clock.now += 4
        self.assertEqual(sampler.measure(), first)
        self.clock.now += 6  # ten seconds after the reading: the change since then
        self.machine.energy(1_000_000 + 200_000_000)
        self.assertEqual(sampler.measure(), (100.0, 20.0, None))
        self.assertEqual(self.clock.slept, [1.0])

    def test_a_reading_older_than_a_minute_is_taken_anew(self):
        sampler = self.machine.sampler(self.clock)
        sampler.measure()
        self.clock.now += 120
        sampler.measure()
        self.assertEqual(self.clock.slept, [1.0, 1.0])

    def test_an_energy_counter_that_wraps_around(self):
        self.machine.energy(262_143_328_850 - 1_000_000)
        sampler = self.machine.sampler(self.clock, lambda: self.machine.energy(4_000_000))
        self.assertEqual(sampler.measure()[1], 5.0)

    @unittest.skipIf(os.geteuid() == 0, "root reads every file")
    def test_an_energy_counter_only_root_may_read(self):
        # The tests own the file, so not even its owner may read it, as RedhAPI may not read root's.
        os.chmod(self.lab.path("sys/class/powercap/intel-rapl:0/energy_uj"), 0)
        _, power, problem = self.machine.sampler(self.clock).measure()
        self.assertIsNone(power)
        self.assertEqual(problem, f"Only root may read {self.lab.path('sys/class/powercap/intel-rapl:0/energy_uj')}; "
                                  "RedhAPI's README says how to let RedhAPI read it.")

    def test_no_energy_counter(self):
        sampler = system.Sampler(self.machine.proc, self.lab.path("nowhere"), clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(sampler.measure()[1:], (None, "There is no energy counter (RAPL) for the processor."))


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        self.machine = Machine(self.lab)

    def test_report(self):
        clock = Clock()
        sampler = self.machine.sampler(clock, lambda: self.machine.ticks(busy=1100, idle=9900))
        report = system.report(sampler, [self.lab.tmp, self.lab.path("nowhere")])
        disks = report.pop("disks")
        self.assertEqual(report, {
            "cpu": {"count": 2, "usage_percent": 10.0, "load": [0.42, 0.38, 0.35], "pressure_percent": 1.25},
            "temperature": {"celsius": 46.0, "sensor": "coretemp: Package id 0"},
            "power": {"watts": 0.0, "sensor": "package-0", "problem": None},
            "memory": {"total_bytes": 16_384_000_000, "available_bytes": 9_216_000_000, "pressure_percent": 0.0},
            "swap": {"total_bytes": 4_096_000_000, "free_bytes": 3_584_000_000},
        })
        self.assertEqual([d["path"] for d in disks], [self.lab.tmp, self.lab.path("nowhere")])
        self.assertGreater(disks[0]["total_bytes"], 0)
        self.assertLessEqual(disks[0]["free_bytes"], disks[0]["total_bytes"])
        self.assertEqual((disks[1]["total_bytes"], disks[1]["free_bytes"]), (None, None))

    def test_a_machine_that_says_nothing(self):
        nowhere = self.lab.path("nowhere")
        clock = Clock()
        report = system.report(system.Sampler(nowhere, nowhere, clock=clock, sleep=clock.sleep), [])
        self.assertEqual(report, {
            "cpu": {"count": None, "usage_percent": None, "load": None, "pressure_percent": None},
            "temperature": {"celsius": None, "sensor": None},
            "power": {"watts": None, "sensor": None, "problem": "There is no energy counter (RAPL) for the processor."},
            "memory": {"total_bytes": None, "available_bytes": None, "pressure_percent": None},
            "swap": {"total_bytes": None, "free_bytes": None},
            "disks": [],
        })


if __name__ == "__main__":
    unittest.main()
