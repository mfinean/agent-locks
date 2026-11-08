"""config.toml: the example parses, the kinds its numbers give, and errors that name the bad setting."""

import pytest

from lockview import config, gate, policy

EXAMPLE = config.EXAMPLE.read_text()


def _load(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return config.load(path)


def test_the_example_gives_the_gates_five_kinds_and_its_thresholds():
    s = config.load(config.EXAMPLE)
    assert (s.gpu.slots, s.gpu.programs, s.remote.hosts) == (2, (), ())
    cfg = policy.from_settings(s)
    assert cfg.kinds == {
        "gpu": policy.Kind("gpu", 2, "16G", 20), "gpu-short": policy.Kind("gpu", 3, "16G", 20, 900), "gpu-solo": policy.Kind("gpu", 3, "16G", 20),
        "cpu": policy.Kind("cpu", 3, "10G", 14), "cpu-short": policy.Kind("cpu", 4, "10G", 14, 120)}
    assert (cfg.solo_patience, cfg.vram_mb, cfg.third_ram) == (7200, 7000, 36)
    assert gate.short_only(gate.slot_table(cfg, [])) == {"gpu.3", "cpu.4"}


def test_programs_and_hosts_are_lists_without_repeats(tmp_path):
    s = _load(tmp_path, EXAMPLE.replace('programs = []', 'programs = ["*/envs/sim/bin/python*"]')
              .replace("hosts = []", 'hosts = ["gpu-box", "lab", "gpu-box"]'))
    assert s.gpu.programs == ("*/envs/sim/bin/python*",) and s.remote.hosts == ("gpu-box", "lab")
    assert policy.from_settings(s).programs == ("*/envs/sim/bin/python*",)


def test_a_machine_without_a_gpu_has_only_the_cpu_kinds(tmp_path):
    s = _load(tmp_path, EXAMPLE[EXAMPLE.index("\n[cpu]\n"):])
    cfg = policy.from_settings(s)
    assert (s.gpu, sorted(cfg.kinds), cfg.programs) == (None, ["cpu", "cpu-short"], ())
    assert [x.name for x in gate.slot_table(cfg, [])] == ["cpu.1", "cpu.2", "cpu.3", "cpu.4"]


def test_no_short_only_slots_means_short_jobs_share_the_others(tmp_path):
    cfg = policy.from_settings(_load(tmp_path, EXAMPLE.replace("short_slots = 1", "short_slots = 0")))
    assert (cfg.kinds["gpu-short"].slots, cfg.kinds["cpu-short"].slots) == (2, 3)
    assert gate.short_only(gate.slot_table(cfg, [])) == set()


@pytest.mark.parametrize(("change", "message"), [
    (("slots = 2 ", "slot = 2 "), r"\[gpu\] has no setting slot$"),
    (("[remote]", "[remotes]"), r"it has no section \[remotes\]$"),
    (("memory_max_gb = 10\n", ""), r"\[cpu\] needs memory_max_gb$"),
    (("\n[cpu]\n", "\n[cpus]\n"), r"it has no section \[cpus\]$"),
    (("slots = 3", "slots = 0"), r"\[cpu\] slots must be 1 or more$"),
    (("ram_needed_gb = 14", "ram_needed_gb = 14.5"), r"\[cpu\] ram_needed_gb must be a whole number, 0 or more, not 14\.5$"),
    (("short_limit_min = 2", "short_limit_min = -1"), r"\[cpu\] short_limit_min must be a number, 0 or more, not -1$"),
    (("ram_needed_gb = 14", "ram_needed_gb = true"), r"must be a whole number, 0 or more, not True$"),
    (("hosts = []", 'hosts = "gpu-box"'), r"\[remote\] hosts must be a list of strings, not 'gpu-box'$"),
    (("hosts = []", "hosts = 1 +"), r"config\.toml: .* \(at line \d+, column \d+\)$"),
])
def test_a_bad_setting_is_named(tmp_path, change, message):
    assert EXAMPLE.count(change[0]) >= 1
    with pytest.raises(config.ConfigError, match=message):
        _load(tmp_path, EXAMPLE.replace(*change, 1))


def test_a_missing_file_says_where_the_example_is(tmp_path):
    with pytest.raises(config.ConfigError, match=r"config\.toml is missing: copy .*share/config\.example\.toml there"):
        config.load(tmp_path / "config.toml")
