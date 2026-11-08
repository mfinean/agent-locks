"""The README's screenshots of lockview, taken from a made-up machine.

A week of gate jobs is written as event-log lines and read the way lockview reads the real log, so
the page gets the same state the sampler would give it. A small server serves that state beside the
page, and headless Chrome takes the pictures:

    python3 docs/screenshots.py      # writes docs/images/*.png

It needs Google Chrome or Chromium, and Pillow for the crops.
"""

from __future__ import annotations

import http.server
import json
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lockview import capacity, config, events, gate, journal, locks, owners, policy, resources, runs, server  # noqa: E402

OUT = ROOT / "docs" / "images"
GB = 2**30
HOME = "/home/dev"
WIDTH, HEIGHT, SCALE = 1440, 920, 2  # the app window's default size
SESSIONS = {"rover-14": "rover", "perception-7": "perception", "planner-3": "planner", "sim-assets-2": "sim-assets", "docs-5": "docs-site"}

# kind -> (command, session, worktree, typical seconds, peak GB)
JOBS = {
    "gpu": [
        ("pixi run -e gpu python train.py --config configs/grasp.yaml", "perception-7", "", 2400, 11.0),
        ("python -m sim.rollout --scenes 64 --headless", "rover-14", "", 1500, 9.0),
        ("bash scripts/render_scenes.sh warehouse", "sim-assets-2", "", 1100, 7.5),
    ],
    "gpu-short": [
        ("pixi run -e gpu pytest tests/gpu -q", "rover-14", "pr-212", 360, 5.0),
        ("python tools/smoke_policy.py --steps 200", "planner-3", "", 180, 3.5),
    ],
    "gpu-solo": [("python -m bench.latency --runs 50", "perception-7", "", 720, 6.0)],
    "cpu": [
        ("make test", "rover-14", "", 700, 6.5),
        ("uv run pytest -q -n 6", "planner-3", "", 480, 4.0),
        ("pnpm build", "docs-5", "", 240, 2.5),
    ],
    "cpu-short": [
        ("ruff check .", "planner-3", "", 6, 0.2),
        ("uv run pytest tests/test_config.py -q", "planner-3", "", 35, 0.6),
        ("mypy src", "rover-14", "", 70, 1.2),
    ],
}
SLOT_KINDS = {"gpu.1": {"gpu": 3, "gpu-short": 2}, "gpu.2": {"gpu": 3, "gpu-short": 2}, "gpu.3": {"gpu-short": 1},
              "cpu.1": {"cpu": 3, "cpu-short": 1}, "cpu.2": {"cpu": 3, "cpu-short": 1}, "cpu.3": {"cpu": 2, "cpu-short": 1},
              "cpu.4": {"cpu-short": 1}}


def _owner(session: str, worktree: str = "") -> dict:
    return {"project": SESSIONS[session], "session": session, "worktree": worktree}


def _busy(t: float) -> float:
    """How likely a slot is to be in use at t: a working day, quieter evenings, and a few long runs overnight."""
    h = time.localtime(t).tm_hour
    return 0.9 if 9 <= h < 19 else 0.55 if 19 <= h < 23 else 0.12


class Log:
    """A made-up event log: the lines a gate would write for each job."""

    def __init__(self, cfg: policy.Config):
        self.cfg, self.lines, self.pid = cfg, [], 20000

    def job(self, kind: str, tpl: tuple, queued: float, start: float, end: float, slots: list[str], rng: random.Random,
            waits: list[tuple[float, str, str]] = ()) -> None:
        cmd, session, worktree, _, peak_gb = tpl
        self.pid += rng.randint(3, 400)
        base = {"v": 1, "pid": self.pid, "tick": self.pid}
        limit = self.cfg.limit(kind)
        code = 143 if limit and end - start >= limit else (1 if rng.random() < 0.07 else 0)
        self.lines += [
            {**base, "t": queued, "ev": "queue", "kind": kind, "cwd": f"{HOME}/{SESSIONS[session]}", "cmd": cmd, "gate": "5c1d2e7a",
             "owner": _owner(session, worktree)},
            *({**base, "t": t, "ev": "wait", "why": why, "detail": detail} for t, why, detail in waits),
            {**base, "t": start, "ev": "start", "slots": slots, "unit": f"agent-gate-{self.pid}.scope"},
            {**base, "t": end, "ev": "end", "code": code, "peak": int(peak_gb * rng.uniform(0.8, 1.1) * GB)},
        ]


def week(cfg: policy.Config, now: float, busy_until: dict[str, float], rng: random.Random) -> events.EventLog:
    """Seven days of finished jobs, slot by slot, with a gpu-solo job most afternoons that takes every GPU slot."""
    log = Log(cfg)
    solos = []
    for day in range(7, 0, -1):
        t = time.mktime(time.localtime(now - day * 86400)[:3] + (14, rng.randint(0, 50), 0, 0, 0, -1))
        if rng.random() < 0.8 and t < now - 3600:
            tpl = JOBS["gpu-solo"][0]
            queued = t - rng.uniform(900, 3000)
            end = t + tpl[3] * rng.uniform(0.9, 1.1)
            log.job("gpu-solo", tpl, queued, t, end, ["gpu.1", "gpu.2", "gpu.3"], rng,
                    [(queued, "slots", "no free gpu slot"), (t - 600, "drain", "the GPU drains for a solo job")])
            solos.append((t, end))
    for slot, weights in SLOT_KINDS.items():
        t, stop = now - 7 * 86400 + rng.uniform(0, 1800), busy_until.get(slot, now) - 60
        while t < stop:
            if rng.random() > _busy(t):
                t += rng.uniform(600, 2400)
                continue
            kind = rng.choices(list(weights), list(weights.values()))[0]
            tpl = rng.choice(JOBS[kind])
            run = tpl[3] * rng.lognormvariate(0, 0.3)
            if limit := cfg.limit(kind):
                run = min(run, limit)
            if slot.startswith("gpu") and (clash := next(((a, b) for a, b in solos if t < b and t + run > a), None)):
                t = clash[1] + rng.uniform(5, 60)
                continue
            if t + run > stop:
                break
            waited = 0.0 if rng.random() < 0.6 else min(rng.expovariate(1 / 300), 2400)
            why = rng.choice(["slots", "slots", "slots", "ram"]) if slot.startswith("cpu") else rng.choice(["slots", "slots", "fifo", "vram"])
            log.job(kind, tpl, t - waited, t, t + run, [slot], rng, [(t - waited, why, "")] if waited else [])
            t += run + rng.expovariate(1 / 90)
    out = events.EventLog()
    out.feed(json.dumps(e) for e in sorted(log.lines, key=lambda e: e["t"]))
    return out


def live(cfg: policy.Config, now: float) -> list[gate.Gate]:
    """The jobs running and waiting right now."""
    scene = [  # kind, template index, state, slots, minutes since it queued, minutes since it started, memory GB now, VRAM MiB
        ("gpu", 0, "running", ["gpu.1"], 31, 22, 9.8, 8900),
        ("gpu", 1, "running", ["gpu.2"], 9, 9, 7.1, 6200),
        ("gpu-short", 0, "running", ["gpu.3"], 2, 2, 3.9, 3100),
        ("gpu-solo", 0, "waiting", [], 41, None, 0, 0),
        ("gpu", 2, "waiting", [], 6, None, 0, 0),
        ("cpu", 0, "running", ["cpu.1"], 5, 5, 2.2, 0),
        ("cpu", 1, "running", ["cpu.2"], 3, 3, 3.1, 0),
        ("cpu", 2, "running", ["cpu.3"], 1.5, 1, 1.4, 0),
        ("cpu-short", 0, "running", ["cpu.4"], 0.1, 0.05, 0.1, 0),
        ("cpu", 1, "waiting", [], 1, None, 0, 0),
    ]
    gates = []
    for i, (kind, n, state, slots, queued_min, started_min, mem_gb, vram) in enumerate(scene):
        cmd, session, worktree, _, peak_gb = JOBS[kind][n]
        if kind == "cpu" and state == "waiting":
            cmd = "uv run pytest tests/integration -q"
        queued = now - queued_min * 60
        pid = 48211 + 37 * i
        scope = f"/user.slice/user-1000.slice/user@1000.service/app.slice/agent-gate-{pid}.scope" if state == "running" else ""
        g = gate.Gate(pid=pid, kind=kind, state=state, needs_idle=kind == "gpu-solo", queued_at=queued,
                      start_ticks=int((queued - now + 3 * 86400) * 100), command=shlex.split(cmd), cwd=f"{HOME}/{SESSIONS[session]}",
                      owner=owners.Owner(**_owner(session, worktree)), old_copy=False, vram_need_mb=cfg.vram_mb, slots=slots,
                      started_at=now - started_min * 60 if started_min is not None else None, scope=scope, vram_mb=vram)
        if state == "running":
            g.stats = resources.ScopeStats(scope.rsplit("/", 1)[-1], int(mem_gb * GB), int(max(mem_gb, peak_gb * 0.9) * GB),
                                           int(float(cfg.kinds[kind].mem.rstrip("G")) * GB))
        gates.append(g)
    gates[5].flags.append("holding cpu.1 while waiting 3 min for ~/rover/.cache/assets.lock")
    return gates


def state(now: float) -> dict:
    rng = random.Random(7)
    cfg = policy.from_settings(config.load(config.EXAMPLE))
    gates = live(cfg, now)
    busy_until = {s: g.started_at for g in gates if g.state == "running" for s in g.slots}
    log = week(cfg, now, busy_until, rng)
    finished = runs.merge(journal.History(), log)
    gpu = resources.Gpu(True, "", 24564, 18750, 91)
    gs = gate.GateState(True, "", cfg, gates, gate.slot_table(cfg, gates), [], 27, gpu)
    gate.explain_waits(gs, now)
    est = server.estimator(finished, {})

    home = str(Path.home())
    holder = locks.Party(51877, now - 260, "python tools/bake_assets.py --all", owners.Owner("sim-assets", "sim-assets-2"))
    waiter = locks.Party(48420, now - 190, "flock .cache/assets.lock make assets", owners.Owner("rover", "rover-14"))
    held = server.lock_rows([locks.Lock(f"{home}/rover/.cache/assets.lock", [holder], [waiter])], est, now)

    return {
        "now": now,
        "sample_ms": 84,
        "machine": {"ram_available_gb": 27, "ram_total_gb": 62.6, "gpu": {**gpu.__dict__, "proc_mb": None, "free_mb": gpu.free_mb},
                    "pressure": {"some_avg10": 0.4, "some_total_s": 812.0, "full_avg10": 0.1, "full_total_s": 140.0},
                    "oomd": {"kills": True, "limit_pct": 50.0, "duration_s": 20.0}, "load": [11.2, 10.4, 9.8], "cpus": 16},
        "sessions": [{"pid": 3100 + i, "name": name, "session_id": f"00000000-0000-4000-8000-00000000000{i}", "project": project,
                      "worktree": "", "status": "busy" if i < 4 else "waiting"} for i, (name, project) in enumerate(SESSIONS.items())],
        "gate": {**gs.as_dict(), "gates": server.gate_jobs(gs, est, now)},
        "locks": held,
        "history": server.history_rows(finished, [], {}, gs, now),
        "journal_error": "", "events_error": "", "machine_log_error": "",
        "capacity": {h: capacity.report(finished, log, gs, now, h) for h in server.CAPACITY_HOURS},
        "remotes": {"config": "~/.config/agent-locks/config.toml", "hosts": [_remote(now)]},
    }


def _remote(now: float) -> dict:
    job = {"pid": 90412, "user": "dev", "command": "python train.py --config configs/fleet.yaml --gpus 2", "start": now - 5400,
           "cpu": 7.6, "rss": 21 * GB, "vram_mb": 38200, "procs": 14, "cwd": "/home/dev/perception", "session": None,
           "ssh": ["192.0.2.10", 51544], "top": [], "owner": _owner("perception-7"), "via": "ssh from here, pid 48977"}
    reading = {"v": 1, "t": now - 1, "host": "gpu-box", "home": "/home/dev", "cpus": 32, "load": [9.1, 8.7, 8.2], "cpu_pct": 28.5,
               "ram_total": 126 * GB, "ram_available": 71 * GB, "pressure": {"some": 0.0, "full": 0.0},
               "gpus": [{"index": i, "name": "NVIDIA RTX 6000 Ada Generation", "total_mb": 49140, "used_mb": used, "util_pct": util}
                        for i, (used, util) in enumerate([(19400, 96), (18800, 94)])],
               "gpu_error": "", "sessions": [], "jobs": [job]}
    return {"alias": "gpu-box", "connected": True, "error": "", "age": 1.2, "reading": reading}


# Run after the page's first render: hide the tooltip and detail panel, and note where each card is.
MEASURE = """<script>
addEventListener("load", () => setTimeout(() => {
  const r = (e) => { const b = e.getBoundingClientRect(); return [b.left, b.top + scrollY, b.width, b.height]; };
  const cards = {};
  document.querySelectorAll("section.card").forEach((s) => { cards[s.querySelector("h2").firstChild.textContent.trim()] = r(s); });
  const pre = document.createElement("pre");
  pre.id = "measured";
  pre.textContent = JSON.stringify({cards, height: document.documentElement.scrollHeight});
  document.body.append(pre);
}, 1500));
</script>"""


def serve(body: bytes) -> http.server.ThreadingHTTPServer:
    page = (ROOT / "lockview" / "web" / "index.html").read_text()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path, _, theme = self.path.partition("?theme=")
            if path == "/api/state":
                return self._send("application/json", body)
            # The page keeps its theme in localStorage: set it before the page's own script reads it.
            pin = f'<script>localStorage.setItem("lockview.theme", {json.dumps(json.dumps(theme or "light"))});</script>'
            self._send("text/html; charset=utf-8", page.replace("<head>", "<head>" + pin, 1).replace("</body>", MEASURE + "</body>").encode())

        def _send(self, ctype: str, data: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def chrome(url: str, *args: str) -> str:
    exe = next((c for c in ("google-chrome", "chromium", "chromium-browser") if shutil.which(c)), None)
    if not exe:
        sys.exit("needs Google Chrome or Chromium")
    with tempfile.TemporaryDirectory() as profile:  # never the user's own profile
        return subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}",
                               "--virtual-time-budget=4000", *args, url], capture_output=True, text=True, timeout=120, check=True).stdout


CARDS = {"Gate queue": "queue", "Slots and locks over time": "timeline", "Gate capacity": "capacity", "Other machines": "machines"}
CARD_RADIUS = 10  # px, as the page's .card


def rounded(img, radius: int):
    """The image with its corners cut round and see-through."""
    from PIL import Image, ImageDraw

    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def framed(img, theme: str):
    """The app window: round corners, a hairline edge and a soft shadow, on a see-through margin."""
    from PIL import Image, ImageDraw, ImageFilter

    radius, margin = 12 * SCALE, 28 * SCALE
    w, h = img.size
    shadow = Image.new("RGBA", (w + 2 * margin, h + 2 * margin), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((margin, margin + 6 * SCALE, margin + w, margin + h + 6 * SCALE), radius, fill=(0, 0, 0, 70))
    out = shadow.filter(ImageFilter.GaussianBlur(14 * SCALE))
    out.alpha_composite(rounded(img, radius), (margin, margin))
    edge = (0, 0, 0, 40) if theme == "light" else (255, 255, 255, 45)
    ImageDraw.Draw(out).rounded_rectangle((margin, margin, margin + w - 1, margin + h - 1), radius, outline=edge, width=SCALE)
    return out


def main() -> None:
    from PIL import Image

    httpd = serve(json.dumps(state(time.time()), default=str).encode())
    OUT.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        url = f"http://127.0.0.1:{httpd.server_address[1]}/?theme={theme}"
        dom = chrome(url, f"--window-size={WIDTH},{HEIGHT}", "--dump-dom")
        measured = json.loads(re.search(r'<pre id="measured">(.*?)</pre>', dom, re.S)[1].replace("&quot;", '"'))
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "page.png"
            chrome(url, f"--window-size={WIDTH},{int(measured['height'])}", f"--force-device-scale-factor={SCALE}", f"--screenshot={shot}")
            page = Image.open(shot)
            framed(page.crop((0, 0, WIDTH * SCALE, HEIGHT * SCALE)), theme).save(OUT / f"window-{theme}.png", optimize=True)
            for title, name in CARDS.items():
                x, y, w, h = measured["cards"][title]
                card = page.crop(tuple(round(v * SCALE) for v in (x, y, x + w, y + h)))
                rounded(card, CARD_RADIUS * SCALE).save(OUT / f"{name}-{theme}.png", optimize=True)
    print("\n".join(sorted(str(p.relative_to(ROOT)) for p in OUT.glob("*.png"))))


if __name__ == "__main__":
    main()
