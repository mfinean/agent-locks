import json
import time
from datetime import datetime

import pytest

from lockview import machinelog, resources

T0 = datetime(2026, 10, 24, 12).timestamp()


def _r(ram=40.0, util=0, vram=1000, psi=(0.0, 0.0), load=1.0, cpu=(0, 0), gpu_ok=True):
    gpu = resources.Gpu(True, "", 24564, vram, util) if gpu_ok else resources.Gpu(False, "nvidia-smi failed")
    pressure = {"some_avg10": psi[0], "some_total_s": 9.0, "full_avg10": psi[1], "full_total_s": 3.0} if psi else {}
    return machinelog.Reading(ram, pressure, gpu, load, cpu)


def _lines(directory):
    return [json.loads(line) for f in sorted(directory.glob("machine-*.jsonl")) for line in f.read_text().splitlines()]


def test_one_line_per_window_with_the_lowest_ram_highest_pressure_and_vram_and_mean_gpu_use(tmp_path):
    log = machinelog.MachineLog(tmp_path, every=20)
    log.add(T0, _r(ram=40.0, util=10, vram=1000, psi=(0.5, 0.0), cpu=(100, 1000)))
    log.add(T0 + 10, _r(ram=31.26, util=90, vram=9000, psi=(3.2, 1.1), cpu=(700, 2200)))
    log.add(T0 + 19, _r(ram=35.0, util=50, vram=5000, psi=(1.0, 0.2), cpu=(1000, 3000)))
    assert _lines(tmp_path) == []  # 19 s: not yet
    log.add(T0 + 21, _r(ram=38.0, util=50, vram=4000, psi=(0.0, 0.0), load=7.5, cpu=(1300, 3400)))
    assert _lines(tmp_path) == [{
        "v": 1, "t": round(T0 + 21), "s": 21, "n": 4,
        "ram_min_gb": 31.3, "psi_some_max": 3.2, "psi_full_max": 1.1,
        "gpu_util_mean": 50, "vram_max_mb": 9000,
        "cpu_pct": 50.0,  # 1,200 of the 2,400 ticks since the first reading
        "load1": 7.5,
    }]
    assert log.error == ""


def test_each_window_starts_where_the_last_one_ended(tmp_path):
    log = machinelog.MachineLog(tmp_path, every=20)
    log.add(T0, _r(ram=10.0, util=100, cpu=(0, 1000)))
    log.add(T0 + 21, _r(ram=30.0, util=100, cpu=(1000, 2000)))  # ends the first window
    log.add(T0 + 30, _r(ram=33.0, util=20, cpu=(1200, 3000)))
    log.add(T0 + 42, _r(ram=34.0, util=40, cpu=(1600, 4400)))
    second = _lines(tmp_path)[1]
    # The reading that ended the first window belongs to it, and its CPU time starts the second.
    assert (second["s"], second["n"], second["ram_min_gb"], second["gpu_util_mean"], second["cpu_pct"]) == (21, 2, 33.0, 30, 25.0)


def test_readings_without_the_gpu_or_pressure_are_left_out_of_them(tmp_path):
    log = machinelog.MachineLog(tmp_path, every=20)
    log.add(T0, _r(gpu_ok=False, psi=None))
    log.add(T0 + 10, _r(util=40, vram=2000, psi=None))
    log.add(T0 + 20, _r(gpu_ok=False, psi=None))  # not averaged in as 0% busy
    log.add(T0 + 30, _r(gpu_ok=False, psi=(2.0, 1.0)))
    log.add(T0 + 40, _r(gpu_ok=False, psi=None))
    first, second = _lines(tmp_path)
    assert (first["gpu_util_mean"], first["vram_max_mb"], first["psi_some_max"], first["psi_full_max"]) == (40, 2000, None, None)
    assert (second["gpu_util_mean"], second["vram_max_mb"], second["psi_some_max"], second["psi_full_max"]) == (None, None, 2.0, 1.0)


@pytest.fixture
def london(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_lines_are_appended_to_the_local_months_file_across_restarts(tmp_path, london):
    oct1 = datetime(2026, 10, 1, 0, 0, 10).timestamp()  # summer time: 23:00:10 on 30 Sep in UTC
    for start in (oct1 - 640, oct1 - 40):  # a second log, as after lockview restarts
        log = machinelog.MachineLog(tmp_path, every=20)
        log.add(start, _r())
        log.add(start + 20, _r())
    log.add(oct1, _r())
    months = {f.name: [json.loads(line)["t"] for line in f.read_text().splitlines()] for f in tmp_path.iterdir()}
    assert months == {"machine-2026-09.jsonl": [round(oct1 - 620), round(oct1 - 20)], "machine-2026-10.jsonl": [round(oct1)]}


def test_a_day_of_lines_stays_under_a_megabyte(tmp_path):
    log = machinelog.MachineLog(tmp_path, every=20)
    widest = _r(ram=62.58, util=100, vram=24564, psi=(99.99, 99.99), load=123.45, cpu=(10**12, 10**12 + 7))
    log.add(T0, _r(cpu=(0, 1)))
    log.add(T0 + 999, widest)
    line = (tmp_path / "machine-2026-10.jsonl").read_bytes()
    assert line.count(b"\n") == 1
    assert 86400 / 20 * len(line) < 1e6


def test_a_failed_write_is_reported_and_the_next_one_tries_again(tmp_path):
    (tmp_path / "file").write_text("")
    log = machinelog.MachineLog(tmp_path / "file" / "lockview", every=20)
    log.add(T0, _r())
    log.add(T0 + 20, _r())
    assert log.error.startswith(f"{tmp_path / 'file' / 'lockview'}/machine-2026-10.jsonl: ")
    (tmp_path / "file").unlink()
    log.add(T0 + 40, _r())
    assert log.error == ""
    assert [(d["t"], d["n"]) for d in _lines(tmp_path / "file" / "lockview")] == [(round(T0 + 40), 1)]


def test_cpu_times_leave_out_idle_and_iowait_and_count_guest_time_once(tmp_path):
    stat = tmp_path / "stat"
    stat.write_text("cpu  100 20 30 400 50 6 7 8 90 10\ncpu0 1 1 1 1 1 1 1 1 1 1\n")
    assert resources.cpu_times(stat) == (171, 621)
