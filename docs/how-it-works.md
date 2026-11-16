# How agent-locks works

The details behind the [README](../README.md): the gate's rules, its event log, and how lockview
reads the machine.

## The gate

### The queue and the slots

Each `agent-gate` call writes a ticket to `$XDG_RUNTIME_DIR/agent-gate/queue/<pid>-<tick>.json`: its
kind, its command, when it queued, who queued it, and once it runs, its slots. A ticket whose process
has gone, or whose pid now belongs to another process, is stale, and the next gate that reads the
queue removes it.

The slots are `flock` locks on `$XDG_RUNTIME_DIR/agent-gate/<lock>.<n>.lock`: `gpu.1` to `gpu.3` and
`cpu.1` to `cpu.4` with the default settings. A kind with n slots takes `<lock>.1` to `<lock>.n`, so
the short-only slots (`gpu.3`, `cpu.4`) come after the others. A gpu-solo job takes every GPU slot.
The job inherits its gate's locks, so it keeps its slot even if its gate is killed.

The job runs as `agent-gate-<pid>.scope` with `MemoryMax` set to its kind's cap, no swap, and
`CPUWeight=20`. When every core is busy, the desktop and other apps (weight 100) get five times a
job's share of the CPU; when they're idle, the job still has the whole CPU. A short kind's scope
also has `RuntimeMaxSec`: systemd sends SIGTERM at the limit and SIGKILL 30 s later, whether or not
the gate is still alive.

### The rules

A waiting gate looks every 15 seconds and rereads `config.toml` each time. It starts when it has a
free slot and none of these checks fails, in this order:

1. **RAM.** Available RAM is at least the kind's `ram_needed_gb`. A gpu-short job taking the
   short-only slot needs `short_slot_ram_gb`.
2. **First come, first served.** No older GPU job is waiting. A gpu-short job may pass gpu jobs for
   the short-only slot, which they can't use. Jobs pass a waiting gpu-solo job until it has waited
   `solo_patience_min`.
3. **Programs outside the gate.** None of the `programs` in `config.toml` runs outside a gate's
   scope.
4. **A quiet GPU.** A gpu-solo job also waits until none of those programs runs at all.
5. **The drain.** Once a gpu-solo job has waited its patience, the GPU drains for it: only gpu-short
   jobs still start, and only while a gpu job runs, since the solo job must wait for that anyway.
6. **VRAM.** Free VRAM is at least `vram_needed_mb`, or `$AGENT_GATE_VRAM_MB` for a job that sets
   it.

The checks after RAM apply only to GPU jobs. CPU jobs take a free slot as soon as RAM allows, in no
fixed order. A cpu-short job tries `cpu.4` first, so it doesn't take a slot a cpu job could use.

The rules are `lockview/policy.py`. lockview calls the same functions to explain a wait, so the
reasons it shows are the gate's own.

### Memory and the thrash watchdog

A job at its `MemoryMax` doesn't die. The kernel reclaims its mapped files instead, the job
thrashes, and that stall counts in the memory pressure of your whole session, which systemd-oomd
watches. With Ubuntu's defaults, oomd kills the session after 20 s above 50%. So the gate watches its job's own pressure and
kills the job once it has stalled on memory for more than half of each of three seconds in a row.
The gate then exits with 137 and says why.

### What callers can rely on

- The command line, `agent-gate <kind> [--] <command…>`, and the five kinds.
- The exit code: the job's own, or 143 or 137 when a short kind's limit stops it, 137 when the thrash
  watchdog kills it, and 128+n for a signal. A bad command line, a missing or invalid `config.toml`,
  or a kind the file has no section for exits with 2 before anything is queued.
- `agent-gate status`: a header, then one line per gate starting with its pid, then one line about
  the machine.
- The pid: the launcher `exec`s, so the pid you started is the gate's pid in the queue and the log.
- The environment: the job gets the caller's environment unchanged. The gate itself runs on
  `/usr/bin/python3 -I`, so your `PYTHONPATH` can't change its imports.
- The event log's format, version 1.

### Updating the gate

`make install` puts a new copy in place. A gate keeps running the code it started with, so a job
that is already waiting keeps the old rules until it starts. lockview marks such jobs "old gate copy".

## The event log

Each gate appends what happens to its job to `~/.local/state/agent-gate/events-<YYYY-MM>.jsonl`
(under `$XDG_STATE_HOME` if set), one JSON object per line. A busy day of about 250 jobs writes a few
hundred KB. Every line has:

| Field | Meaning |
|---|---|
| `v` | schema version, 1 |
| `t` | epoch seconds |
| `ev` | the event (below) |
| `pid` | the gate's pid |
| `tick` | the gate's start time in clock ticks since boot; with `pid`, it names one gate |

| `ev` | Fields | When |
|---|---|---|
| `queue` | `kind`, `cwd`, `cmd` (first 2,000 characters), `gate` (first 8 hex digits of the sha1 of the gate's code), `owner` (`project`, `session`, `worktree`) | the gate starts |
| `wait` | `why`, `detail` (the reason as text) | each time the reason it can't start changes |
| `start` | `slots` (e.g. `["gpu.1"]`), `unit` (`agent-gate-<pid>.scope`) | the job starts |
| `end` | `code` (the job's exit code), `peak` (bytes, read inside the job's scope as its last act, or `null`) | the job ends |

| `why` | Meaning |
|---|---|
| `slots` | no free slot |
| `ram` | too little RAM available |
| `fifo` | an older GPU job waits |
| `program` | a gated program runs outside the gate |
| `solo` | a gpu-solo job needs no gated program running |
| `drain` | the GPU drains for a solo job past its patience |
| `vram` | too little VRAM free |

What the log leaves out:

- **A gate killed while it waits** logs no `end`.
- **A gate killed while its job runs** logs no `end` either. The job keeps running, and the user
  journal still has its scope's end.
- **OOM kills** come from the journal's events for the same unit.

Logging is best-effort, so it never delays or fails a job. A directory it can't write, a full disk or
a file lock it can't get within a second all mean a missing line. Readers should ignore fields and
`ev` values they don't know.

## lockview

lockview samples the machine every 3 seconds; one sample takes about 100 ms. It serves the page from
`127.0.0.1` on a free port and opens it in a GTK WebKit window, or with `--browser` in Chrome or your
default browser.

### What it reads

| Source | Gives |
|---|---|
| `config.toml` | the kinds, slots, memory caps, thresholds, gated programs and other machines |
| the gate's tickets | each job's kind, state, slots, queue time and owner, and whether it runs an older copy of the gate |
| each job's cgroup | memory now, its peak and its cap; VRAM comes from `nvidia-smi` per process |
| `/proc/locks` and open file descriptors | holders and waiters of other exclusive `flock` locks |
| each process's `/proc/<pid>/stat` | its CPU time, memory and session, for heavy jobs outside the gate |
| each process's `NSpid`, working directory and mount namespace | which processes are containers' inits, and which Bazel client a Bazel server works for |
| the Docker API, `/var/run/docker.sock` | each container's name, and the command `docker exec` gave each process it started |
| the gate's event log | each finished job's kind, slots, waits by reason, exit code and peak memory, two weeks back |
| `journalctl --user` | every finished `systemd-run` scope: start, end, CPU time and OOM kills |
| `~/.claude/sessions/*.json` | which Claude Code session started a process |
| `/proc/net/tcp` and `tcp6` | which local ssh client started a job on another machine |

It never takes a lock, runs the gate, removes a ticket or signals a process.

### Who owns a job

A job's owner is its session, its project and its worktree.

- **The session** is the nearest ancestor process that Claude Code registered in
  `~/.claude/sessions/`.
- **The project** is the git repository the job runs in. A linked worktree belongs to its main
  repository, and its directory name is shown as the worktree. Outside git, it is the session's
  project, or else the first directory under your home.
- **A session's scratchpad** belongs to that session, even if it holds a repository.

The gate records the owner when the job queues. For a run it didn't log, lockview uses what it saw
while the job ran (kept in `~/.local/state/lockview/observed.json`), or else the job's directory.

### Jobs outside the gate

lockview applies the probe's test (see [Other machines](#other-machines)) to this machine too. A job
is a process tree that something started: the topmost process below an init, sshd, tmux, screen, a
Claude session or an interactive shell, with its descendants. A container's init is one if all it does
is start or wait (`docker-init`, `tini`, `sleep infinity`, an interactive shell); one that does the
work, as in `docker run img make`, is the job. It is heavy if it
uses a GPU, half a core or 2 GB, with its CPU use measured between two samples. agent-gate is a
boundary too, as what it runs is a gate job. Gate jobs are left out, with any job they drive. Jobs in
containers and jobs driven from another job are listed, and so is each job a Claude session started;
any other job, such as the desktop's own apps, is left out.

A job's CPU use counts processes that start and end between two samples, such as a make's compiles:
each process is also charged the CPU time of the children it reaped since the last sample, less what
those children had used by then, which that sample counted. Sessions, shells and the other processes
that start jobs are charged their own time alone, since the children they reap were jobs of their own,
and so is a process in the interval it reaps one of them: the shell that ran agent-gate, as agent-gate
ends with its gate job. A process that is exiting is still read, so its time is counted once, however
the samples fall.

### Jobs in containers and under Bazel

Some jobs are driven from another job, and belong to whoever started that one:

- **`docker exec`.** The process an exec starts is a child of the container's shim, not of the
  `docker` client that asked for it. The Docker API gives the exec's command and the process it
  started. Its client is the `docker exec` here for that container with that command. Of several,
  the newest exec takes the newest client that started before it, and so on back, as execs start in
  the order their clients did.
- **A Bazel server** runs as a daemon under its own init, and works for the Bazel client whose command
  it runs. Its client is the `bazel` (or apt's `bazel-real`) whose directory is in the server's
  workspace (the innermost, if workspaces nest), in its mount namespace, and under its
  `--output_user_root` and with its `--output_base` if the client gives them. A client that fits two
  servers drives neither, as nothing tells which. Of a server's clients, the oldest drives it, and the
  others wait for it. bazelisk runs the client as a `bazel` of its own, so a client that runs another
  isn't one.

Links chain: a test's Bazel server is driven by the Bazel client in the exec'd test, which is driven
by the `docker exec` a Claude session runs. The job belongs to that session, and "Started by" lists
each link. A container's own job, which nothing here drives (a service it runs, or a Bazel server
between commands), is listed under the container's name.

The Docker API is asked about each container and each exec'd process once, in about 5 ms.
Without it, containers go by their ids and execs aren't traced to their clients, and the page
says why.

### Estimates and predicted starts

A job's command is reduced to two keys:

- **The command, masked:** paths cut to their last part (a script keeps its directory, so two
  scripts called `run.sh` stay apart), and hashes, numbers and temporary paths masked.
- **What it runs:** for example `pixi:dev:test`, `scripts/render_scenes.sh` or `make test`.

The estimate is the median of the last 20 finished runs of the most specific key with history, with
the 90th percentile as its spread. A run ended by a signal only says the job takes at least that
long, so it doesn't count; a failing run does, since a failing test run takes as long as a passing
one. A command with no history gets the median first run of its kind, marked **guess**.

Start times come from playing the gate's rules forward: running jobs free their slots at their
estimated end, waiters start in turn, short jobs take their own slot, and a gpu-solo job drains the
GPU once it has waited its patience. RAM, VRAM and program checks are assumed to pass, since they
can't be predicted.

### Capacity

The **Gate capacity** card sums up the past 24 hours, 3 days or 7 days from the event log and the
live queue:

- **Slots busy:** slot time held per lock, with the short-only slots counted apart.
- **GPU jobs waiting:** the time at least one GPU job waited, how much of it every slot was busy,
  and the slot time left free while a job waited (drains, or the RAM, VRAM and program checks).
- **GPU jobs' memory together:** the most that GPU jobs running at once used, as the sum of each
  job's own peak. It is an upper bound, since their peaks need not coincide. Compare it with
  `short_slot_ram_gb`.
- **By kind:** waits by reason, run times, peaks, failures and runs stopped at the limit.

### Machine history

lockview keeps a record of the machine's RAM, memory pressure, GPU and CPU use. About every 20
seconds it appends a line to `~/.local/state/lockview/machine-<YYYY-MM>.jsonl`, about 650 KB a day:

| Field | Meaning |
|---|---|
| `v` | schema version, 1 |
| `t`, `s`, `n` | the time of the last reading, the seconds the line covers, and how many readings it sums |
| `ram_min_gb` | the lowest available RAM (GiB) |
| `psi_some_max`, `psi_full_max` | the highest memory pressure (`avg10`, %) of your session's cgroup, the one systemd-oomd watches |
| `gpu_util_mean`, `vram_max_mb` | mean GPU use (%) and the most VRAM in use (MiB), or `null` without `nvidia-smi` |
| `cpu_pct`, `load1` | the share of CPU time spent busy, and the 1-minute load average |

It records only while lockview runs, so a gap between lines is time it was closed.

### Other machines

List ssh aliases under `[remote] hosts` and restart lockview. For each host it keeps one ssh
connection open in `BatchMode`, so it never prompts: the host needs key login and `python3` 3.8 or
later. The connection sends `lockview/probe.py` as python3's program on stdin, and the probe prints a
reading every 3 seconds. Nothing is installed on the host. When lockview exits, the probe's next write
fails and it exits too.

The probe reports the host's RAM, memory pressure, GPUs, CPU use, and its heavy jobs: process trees
using a GPU, half a core or 2 GB. It links jobs in containers and under Bazel to the processes that
drive them, as lockview does here (see [Jobs in containers and under Bazel](#jobs-in-containers-and-under-bazel)).
A job's owner is the owner of the process at the far end of its links, its origin, or of its own
root without any. In order, that is:

1. the Claude Code session on that host that the origin runs under,
2. the local ssh client whose connection started the origin, found from `SSH_CONNECTION` and the
   client's socket, and that client's session,
3. for a container's own job, the container, or
4. the project of the origin's directory.

### Limits

- **Lock waiters' order.** `flock` wakes waiters in no fixed order, so their predicted starts assume
  they start in the order they arrived.
- **Runs the log lacks.** A job whose log lines were lost is known from the journal alone, with a
  guessed kind. The timeline puts it in a free slot, marked `?`, which shows how busy the slots were
  but not which slot it had.
- **Very short scopes** can end without the journal's CPU-time line, and are left out.
- **Gate runs shorter than a sample.** An agent-gate that starts and ends between two samples is
  never seen, so for that sample its gate job's CPU counts toward the job that ran agent-gate.
- **Detached jobs here.** A job started with `setsid nohup … &` leaves its session's process tree,
  so it isn't listed outside the gate.
- **Execs without a client.** `docker exec -d` leaves no client to trace, and neither
  `docker compose exec` nor a client given options before `exec` (`docker -H … exec`, `--context`)
  is read as one, so the job is the container's.
- **Containers run as root.** lockview reads another user's processes' directories and mount
  namespaces only as root, so a Bazel server in a container whose processes run as root isn't linked
  to its client.
- **Detached remote jobs.** lockview sees a job's ssh client only if the client is still running at
  one of its readings. It remembers clients for a day, which covers `setsid nohup … &`.
- **Other machines' history** isn't kept: lockview shows what runs there now.
