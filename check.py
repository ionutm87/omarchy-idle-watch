#!/usr/bin/env python3
"""Idle watch: flags hung processes and heavy CPU/GPU use while nobody is at the keyboard.

Prints {"issues": [...]} for the bar widget and sends one notification per new issue.
Several bar instances (one per monitor) may call this at once; a lock plus a 30 s
result cache keeps the per-process CPU deltas measured over a real interval.
"""
import argparse, datetime, fcntl, glob, json, os, re, shutil, signal, subprocess, time

STATE = os.environ.get("IDLE_WATCH_STATE", os.path.expanduser("~/.local/state/omarchy/idle-watch.json"))
ACKS = STATE.replace(".json", "-ack.json")  # faults the user chose to hide
HZ = os.sysconf("SC_CLK_TCK")


def plural(n, one, many=None):
    return f"{n} {one if n == 1 else many or one + 's'}"


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def processes():
    out = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        stat = read(f"/proc/{pid}/stat")
        if not stat:
            continue
        f = stat[stat.rindex(")") + 2:].split()  # f[n] is stat field n+3
        out[pid] = {
            "comm": stat[stat.index("(") + 1:stat.rindex(")")],
            "ppid": f[1],
            "cpu": (int(f[11]) + int(f[12])) / HZ,
            "start": int(f[19]) / HZ,
            "state": f[0],
            "threads": int(f[17]),
        }
    return out


def label(pid, p):
    cmd = read(f"/proc/{pid}/cmdline").replace("\0", " ").strip() or p["comm"]
    return f"{cmd[:70]} (PID {pid})"


def orphaned(p, procs):
    # Parent gone: reparented to init or to the user's systemd (subreaper).
    return p["ppid"] == "1" or procs.get(p["ppid"], {}).get("comm") == "systemd"


def cpu_temp():
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        if read(f"{hw}/name") == "coretemp":
            return int(read(f"{hw}/temp1_input") or 0) / 1000
    return 0


def nvidia_util():
    # nvidia-smi wakes a runtime-suspended dGPU, so only ask when it's already awake.
    if "active" not in [read(p) for p in glob.glob("/sys/bus/pci/drivers/nvidia/*/power/runtime_status")]:
        return 0
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5)
        return max(int(x) for x in r.stdout.split())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def find_issues(a, procs, prev, interval, uptime):
    issues = []
    for pid, p in procs.items():
        age = uptime - p["start"]
        if age >= a.hung_minutes * 60 and p["cpu"] / max(age, 1) >= 0.5 and orphaned(p, procs):
            issues.append({"key": f"hung:{pid}:{p['start']}", "kind": "hung", "pid": int(pid), "start": p["start"],
                           "text": f"Hung for {int(age // 60)} min, CPU {p['cpu'] / age * 100:.0f}%: {label(pid, p)}",
                           "hint": f"Process PID {pid} lost its parent and keeps the CPU busy. Find out what it is doing "
                                   f"(cmdline, cwd, `strace -p {pid}`, py-spy if it is Python) and what started it."})
    if not a.idle:
        return issues
    if interval:
        for pid, p in procs.items():
            old = prev.get(pid)
            if old and old[0] == p["start"]:
                pct = (p["cpu"] - old[1]) / interval * 100
                key = f"cpu:{pid}:{p['start']}"
                if pct >= a.cpu and not any(i["key"].startswith(f"hung:{pid}:") for i in issues):
                    issues.append({"key": key, "kind": "cpu", "pid": int(pid), "start": p["start"], "text": f"CPU {pct:.0f}% while you were away: {label(pid, p)}",
                                   "hint": f"Find out what PID {pid} does while I am away from the computer and whether that is normal."})
    gpu = nvidia_util()
    if gpu >= a.gpu:
        issues.append({"key": "gpu", "kind": "gpu", "text": f"NVIDIA GPU {gpu}% while you were away",
                       "hint": "Use `nvidia-smi` to see which process is using the GPU."})
    temp = cpu_temp()
    if temp >= a.temp:
        issues.append({"key": "temp", "kind": "temp", "text": f"CPU at {temp:.0f} °C while you were away",
                       "hint": "Find out what is heating the CPU (processes, fans, power profile)."})
    return issues


STATES = {"R": "active (running)", "S": "waiting", "I": "waiting", "D": "blocked on I/O",
          "Z": "zombie", "T": "stopped", "t": "stopped (debug)"}


def human_age(s):
    s = int(s)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m" if m else f"{s}s"


def suspicions(pid, p, procs, uptime):
    """Cheap red flags; each one is a reason to look closer, not proof."""
    age = uptime - p["start"]
    avg = p["cpu"] / max(age, 1)
    exe = ""
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        pass
    out = []
    if orphaned(p, procs) and avg >= 0.5 and age >= 600:
        out.append(f"no parent and {avg * 100:.0f}% CPU on average over {human_age(age)} (probably hung)")
    if exe.endswith(" (deleted)"):
        out.append("the binary on disk was deleted or replaced (normal after an update)")
    if exe.startswith(("/tmp/", "/dev/shm/", "/var/tmp/")):
        out.append(f"running from a temporary directory: {exe}")
    if p["state"] == "D":
        out.append("blocked on I/O (state D)")
    if p["state"] == "Z":
        out.append("zombie: it has exited, but its parent has not reaped it")
    return out


def sample():
    before = processes()
    time.sleep(1)
    after = processes()
    now = {pid: (p["cpu"] - before[pid]["cpu"]) * 100 for pid, p in after.items()
           if pid in before and before[pid]["start"] == p["start"]}
    return after, now


def rss_mb(pid):
    return int((read(f"/proc/{pid}/statm") or "0 0").split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20


def sockets(pid):
    """Listening ports and established connections of pid, via ss (own processes only)."""
    try:
        out = subprocess.run(["ss", "-Htunap"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return [], 0
    mine = [l.split() for l in out.splitlines() if f"pid={pid}," in l]
    listen = sorted({f"{c[0]} {c[4]}" for c in mine if c[1] in ("LISTEN", "UNCONN")})
    return listen, sum(c[1] == "ESTAB" for c in mine)


def details(pid, start):
    procs, now = sample()
    p = procs.get(str(pid))
    if not p or p["start"] != start:
        print(json.dumps({"gone": True}))
        return
    pid = str(pid)
    uptime = float(read("/proc/uptime").split()[0])
    age = uptime - p["start"]
    status = dict(l.split(":", 1) for l in read(f"/proc/{pid}/status").splitlines() if ":" in l)
    io = dict(l.split(": ") for l in read(f"/proc/{pid}/io").splitlines() if ": " in l)
    link = lambda name: (os.readlink(f"/proc/{pid}/{name}") if os.path.exists(f"/proc/{pid}/{name}") else "")
    try:
        exe, cwd, fds = link("exe"), link("cwd"), len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        exe, cwd, fds = "", "", "—"
    unit = read(f"/proc/{pid}/cgroup").rsplit("/", 1)[-1]
    parent = procs.get(p["ppid"], {})
    children = [f"{c['comm']} ({cpid})" for cpid, c in procs.items() if c["ppid"] == pid]
    listen, estab = sockets(pid)
    uid = status.get("Uid", "0").split()[0]
    try:
        import pwd
        user = pwd.getpwuid(int(uid)).pw_name
    except (KeyError, ValueError):
        user = uid
    mb = lambda b: f"{int(b) / 2**20:.0f} MB"
    print(json.dumps({
        "pid": int(pid), "start": p["start"], "name": p["comm"],
        "cmd": read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()[:600],
        "fields": [
            ["State", STATES.get(p["state"], p["state"]) + f" · {now.get(pid, 0):.0f}% CPU now"],
            ["Started", time.strftime("%d %b %H:%M:%S", time.localtime(time.time() - age)) + f" ({human_age(age)} ago)"],
            ["Avg CPU", f"{p['cpu'] / max(age, 1) * 100:.0f}% · {human_age(p['cpu'])} CPU total"],
            ["RAM", f"{rss_mb(pid):.0f} MB" + (f" · swap {status['VmSwap'].strip()}" if status.get("VmSwap", "0 kB").strip() != "0 kB" else "")],
            ["User", user],
            ["Parent", f"{parent.get('comm', '?')} ({p['ppid']})"],
            ["Children", ", ".join(children[:8]) + (f" +{len(children) - 8}" if len(children) > 8 else "") or "—"],
            ["Threads", str(p["threads"])],
            ["Unit", unit or "—"],
            ["Executable", exe or "—"],
            ["Directory", cwd or "—"],
            ["Open files", str(fds)],
            ["Network", (", ".join(listen[:6]) or "no listening ports") + f" · {plural(estab, 'connection')}"],
            ["Disk I/O", f"read {mb(io['read_bytes'])} · written {mb(io['write_bytes'])}" if io else "—"],
        ],
        "suspect": suspicions(pid, p, procs, uptime),
    }))


def upgrades():
    """Last ALPM upgrade time per package, from pacman's log."""
    last = {}
    try:
        with open("/var/log/pacman.log") as f:
            for line in f:
                if "[ALPM] upgraded " in line:
                    ts = line[1:line.index("]")]
                    last[line.split("upgraded ", 1)[1].split()[0]] = ts
    except OSError:
        pass
    out = {}
    for pkg, ts in last.items():
        when = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone()
        out[pkg] = when.strftime("%d %b %H:%M")
    return out


def owner(path):
    """(package, version) that owns path, or (None, None) when no package does."""
    try:
        r = subprocess.run(["pacman", "-Qo", path], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None, None
    m = re.search(r"is owned by (\S+) (\S+)", r.stdout)
    return (m.group(1), m.group(2)) if m else (None, None)


def desktop_for(pkg):
    """The package's launcher .desktop id, skipping hidden and configure/settings entries."""
    try:
        files = subprocess.run(["pacman", "-Qlq", pkg], capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return ""
    for f in files:
        if f.startswith("/usr/share/applications/") and f.endswith(".desktop"):
            if "NoDisplay=true" not in read(f) and not re.search(r"config|setting", os.path.basename(f), re.I):
                return os.path.basename(f)
    return ""


def stale_groups():
    """Processes still running a binary that was deleted or replaced on disk, grouped by package
    (by file when no package owns it), with how the group can be restarted."""
    procs, now = sample()
    uptime = float(read("/proc/uptime").split()[0])
    owners, groups = {}, {}
    for pid, p in procs.items():
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            continue
        if not exe.endswith(" (deleted)") or int(pid) == os.getpid():
            continue
        path = exe[: -len(" (deleted)")]
        if path not in owners:
            owners[path] = owner(path)
        pkg, ver = owners[path]
        cpu = round(now.get(pid, 0))
        row = {"pid": int(pid), "start": p["start"], "cpu": cpu, "mem": round(rss_mb(pid)), "ppid": int(p["ppid"]),
               "name": p["comm"], "cmd": read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()[:120],
               "age": human_age(uptime - p["start"]), "active": cpu > 0 or p["state"] == "R",
               "suspect": suspicions(pid, p, procs, uptime), "cgroup": read(f"/proc/{pid}/cgroup").split("::", 1)[-1]}
        g = groups.setdefault(pkg or path, {"key": pkg or path, "name": pkg or os.path.basename(path),
                                            "pkg": pkg, "ver": ver, "procs": [], "mem": 0})
        g["procs"].append(row)
        g["mem"] += row["mem"]
    ups = upgrades()
    out = []
    for g in groups.values():
        pids = {r["pid"] for r in g["procs"]}
        roots = [r for r in g["procs"] if r["ppid"] not in pids]
        unit = roots[0]["cgroup"].rsplit("/", 1)[-1] if roots else ""
        if unit.endswith(".service"):
            restart = {"kind": "service", "unit": unit, "user": "/user@" in roots[0]["cgroup"]}
        elif g["pkg"] and desktop_for(g["pkg"]):
            restart = {"kind": "desktop", "id": desktop_for(g["pkg"])}
        else:
            restart = None
        g.update(upgraded=ups.get(g["pkg"], "") if g["pkg"] else "", count=len(g["procs"]), restart=restart)
        g["prompt"] = with_prompt({
            "title": f"{g['name']} is still running from the old binary ({plural(g['count'], 'process', 'processes')}, {g['mem']} MB)",
            "detail": f"package {g['pkg']} {g['ver']}, upgraded {g['upgraded'] or '?'}" if g["pkg"] else f"file {g['key']}",
            "hint": f"PIDs: {', '.join(str(r['pid']) for r in g['procs'][:12])}. Possible restart: {restart or 'unknown'}. "
                    "Tell me how to restart it on the new version without losing data."})["prompt"]
        g["procs"].sort(key=lambda r: r["mem"], reverse=True)
        out.append(g)
    out.sort(key=lambda g: g["mem"], reverse=True)
    return out


def stale():
    print(json.dumps({"groups": stale_groups()}))


def find_group(key):
    return next((g for g in stale_groups() if g["key"] == key), None)


def stop_procs(rows, wait=8):
    """TERM every (pid, start) still alive, KILL whatever outlives `wait` seconds. Returns how many were killed."""
    alive = lambda r: processes().get(str(r["pid"]), {}).get("start") == r["start"]
    rows = [r for r in rows if alive(r)]
    for r in rows:
        try:
            os.kill(r["pid"], 15)
        except OSError:
            pass
    deadline = time.time() + wait
    while time.time() < deadline and any(alive(r) for r in rows):
        time.sleep(0.25)
    forced = [r for r in rows if alive(r)]
    for r in forced:
        try:
            os.kill(r["pid"], 9)
        except OSError:
            pass
    return len(rows), len(forced)


def stop_group(key):
    g = find_group(key)
    if not g:
        return print("the group is no longer running")
    n, forced = stop_procs(g["procs"])
    print(f"{g['name']}: {plural(n, 'process', 'processes')} stopped" + (f" ({forced} forced)" if forced else ""))


def restart_group(key):
    g = find_group(key)
    if not g:
        return print("the group is no longer running")
    r = g["restart"]
    if not r:
        return print(f"{g['name']}: I don't know how to restart it; use Stop")
    if r["kind"] == "service":
        cmd = ["systemctl", "--user", "restart", r["unit"]] if r["user"] else ["pkexec", "systemctl", "restart", r["unit"]]
        res = subprocess.run(cmd, capture_output=True, text=True)
        return print(f"{r['unit']} restarted" if res.returncode == 0 else f"error: {res.stderr.strip()[:120]}")
    n, forced = stop_procs(g["procs"])
    subprocess.Popen(["uwsm-app", "--", r["id"]], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"{g['name']}: {plural(n, 'process', 'processes')} stopped" + (f" ({forced} forced)" if forced else "") + f", restarted via {r['id']}")


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def when(ts):
    t = time.localtime(ts)
    return time.strftime("%H:%M" if t.tm_yday == time.localtime().tm_yday else "yesterday %H:%M", t)


def last_error(user, unit):
    """The service's own most telling recent log line (systemd's 'Failed with result' lines say little)."""
    lines = [l.strip() for l in run(["journalctl"] + (["--user"] if user else []) +
                                    ["-u", unit, "-n", "30", "-o", "cat", "--no-pager"]).splitlines()]
    lines = [l for l in lines if l and not l.startswith(unit + ":")]
    bad = [l for l in lines if re.search(r"error|cannot|can't|failed|not found|no such|denied|exception|fatal", l, re.I)]
    return (bad or lines or [""])[-1][:200]


def unit_faults():
    out = []
    for user in (True, False):
        base = ["systemctl"] + (["--user"] if user else [])
        try:
            rows = json.loads(run(base + ["list-units", "--all", "-o", "json", "--plain", "--state=failed,auto-restart"]) or "[]")
        except ValueError:
            rows = []
        for u in rows:
            name = u["unit"]
            n = run(base + ["show", "-p", "NRestarts", "--value", name]).strip()
            looping = u["sub"] == "auto-restart"
            out.append({
                "key": f"unit:{'user' if user else 'system'}:{name}", "kind": "unit", "urgent": True,
                "title": f"{name} " + (f"keeps crashing and restarting in a loop ({plural(n, 'restart')})" if looping else "failed"),
                "detail": last_error(user, name) or u["description"],
                "hint": f"Check `systemctl{' --user' if user else ''} status {name}` and "
                        f"`journalctl{' --user' if user else ''} -u {name} -n 80`; look at the unit file and what is missing.",
                "actions": [{"id": "restart", "icon": "", "tip": "Restart the service", "confirm": "Yes, restart"},
                            {"id": "disable", "icon": "", "tip": "Stop and disable the service",
                             "confirm": "Yes, disable"}]})
    return out


def crash_faults():
    out = []
    try:
        dumps = json.loads(run(["coredumpctl", "list", "--since=-24h", "--json=short", "--no-pager"]) or "[]")
    except ValueError:
        dumps = []
    for c in dumps:
        try:
            sig = signal.Signals(c["sig"]).name
        except ValueError:
            sig = f"signal {c['sig']}"
        out.append({"key": f"crash:{c['time']}", "kind": "crash", "urgent": True,
                    "title": f"{os.path.basename(c['exe'])} crashed ({sig}) at {when(c['time'] / 1e6)}",
                    "detail": f"{c['exe']} · PID {c['pid']} · details: coredumpctl info {c['pid']}",
                    "hint": f"Read `coredumpctl info {c['pid']}` (backtrace) and the log around that time; "
                            "tell me whether it is a known bug and what I can do (update, config, report).",
                    "actions": [ACK]})
    oom = [["journalctl", "-k", "-g", r"Out of memory: Killed process"],
           ["journalctl", "-u", "systemd-oomd", "-g", "Killed"]]
    for cmd in oom:
        for line in run(cmd + ["--since=-24h", "-o", "json", "--no-pager"]).splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            msg = e.get("MESSAGE", "")
            m = re.search(r"Killed process \d+ \((.+?)\)", msg) or re.search(r"Killed (\S+) due to", msg)
            name = os.path.basename(m.group(1)) if m else "a process"
            out.append({"key": f"oom:{e['__REALTIME_TIMESTAMP']}", "kind": "oom", "urgent": True,
                        "title": f"Memory ran out: {name} was killed at {when(int(e['__REALTIME_TIMESTAMP']) / 1e6)}",
                        "detail": msg[:200], "actions": [ACK],
                        "hint": "See `journalctl -k --since=-24h -g 'Out of memory'` and who is using memory now; "
                                "suggest how to avoid it (swap/zram, limits, the offending app)."})
    return out


def process_faults(procs):
    out, zombies = [], {}
    for pid, p in procs.items():
        if p["state"] == "Z":
            zombies.setdefault(p["ppid"], []).append(p["comm"])
    for ppid, names in zombies.items():
        par = procs.get(ppid, {"comm": "?", "start": 0})
        out.append({"key": f"zombie:{ppid}:{par['start']}", "kind": "zombie", "urgent": False,
                    "pid": int(ppid), "start": par["start"],
                    "title": (f"1 zombie process left" if len(names) == 1 else f"{len(names)} zombie processes left")
                             + f" by {par['comm']} (PID {ppid})",
                    "detail": ", ".join(sorted(set(names))) + f" · uses no resources; goes away when {par['comm']} exits",
                    "hint": f"Parent {par['comm']} (PID {ppid}) does not reap its children. Tell me whether it is a known bug "
                            "and whether it is worth restarting.",
                    "actions": [ACK]})
    # ponytail: D seen in two snapshots 1 s apart; heavy but healthy I/O can show up briefly.
    time.sleep(1)
    later = processes()
    for pid, p in procs.items():
        if p["state"] == "D" and p["ppid"] not in ("0", "2") and later.get(pid, {}).get("state") == "D":
            out.append({"key": f"dstate:{pid}:{p['start']}", "kind": "dstate", "urgent": False,
                        "pid": int(pid), "start": p["start"],
                        "title": f"{p['comm']} (PID {pid}) blocked on I/O",
                        "detail": f"waiting in the kernel at {read(f'/proc/{pid}/wchan') or '?'} · cannot be stopped until it unblocks",
                        "hint": f"See `sudo cat /proc/{pid}/stack`, `dmesg` and which disk, network mount or driver is blocking it.",
                        "actions": [ACK]})
    return out


def disk_faults():
    out, seen = [], set()
    for m in ("/", "/home", "/boot", "/efi"):
        try:
            du = shutil.disk_usage(m)
        except OSError:
            continue
        if (du.total, du.used) in seen:  # btrfs subvolumes share one pool
            continue
        seen.add((du.total, du.used))
        pct = du.used / du.total * 100
        if pct >= 90:
            out.append({"key": f"disk:{m}", "kind": "disk", "urgent": True,
                        "title": f"Disk {m} is {pct:.0f}% full", "detail": f"{du.free / 2**30:.1f} GB free", "actions": [],
                        "hint": f"Find what is taking space on {m} (pacman/yay cache, journal, docker, ~/.cache, downloads) "
                                "and suggest what can be deleted."})
    return out


ACK = {"id": "ack", "icon": "", "tip": "Seen, hide it"}


def acks():
    try:
        with open(ACKS) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def find_faults(procs):
    """Everything broken on the system right now, beyond processes using a lot: failed or
    crash-looping services, crashes and OOM kills in the last 24 h, zombies, stuck I/O, full disks."""
    hidden = acks()
    return [with_prompt(f) for f in unit_faults() + crash_faults() + process_faults(procs) + disk_faults()
            if f["key"] not in hidden]


def with_prompt(f):
    """What the panel's Claude button types into a new Claude Code session for this problem."""
    f["prompt"] = (f"On my Omarchy laptop (Arch Linux, Hyprland) the idle-watch panel reports: "
                   f"{f.get('title') or f.get('text')}.\n" + (f"Details: {f['detail']}\n" if f.get("detail") else "")
                   + f"{f.get('hint', '')}\nDiagnose the cause, explain it briefly and fix it. "
                   "Ask me for confirmation before stopping, deleting, disabling or installing anything.")
    return f


def fix(key, action):
    if action == "ack":
        os.makedirs(os.path.dirname(ACKS), exist_ok=True)
        with open(STATE + ".lock", "w") as lock:  # two quick ✓ clicks must not drop one key
            fcntl.flock(lock, fcntl.LOCK_EX)
            keys = sorted(acks() | {key})
            with open(ACKS, "w") as f:
                json.dump(keys, f)
        return print("hidden")
    kind, scope, unit = key.split(":", 2)
    if kind != "unit" or action not in ("restart", "disable"):
        return print("unknown action")
    if scope == "user":
        cmds = [["systemctl", "--user", "restart", unit]] if action == "restart" else \
               [["systemctl", "--user", "disable", "--now", unit], ["systemctl", "--user", "reset-failed", unit]]
    else:  # one pkexec prompt for the whole action
        script = 'systemctl restart "$1"' if action == "restart" else 'systemctl disable --now "$1"; systemctl reset-failed "$1"'
        cmds = [["pkexec", "sh", "-c", script, "sh", unit]]
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True)
        if r.returncode != 0 and "reset-failed" not in c:
            return print(f"error: {r.stderr.strip()[:160]}")
    print(f"{unit} " + ("restarted" if action == "restart" else "stopped and disabled"))


def kill(pid, start, force):
    """Signal pid only if it is still the process the popup showed (PIDs get reused)."""
    p = processes().get(str(pid))
    if not p or p["start"] != start:
        print("the process no longer exists")
        return
    try:
        os.kill(pid, 9 if force else 15)
        print("KILL sent" if force else "TERM sent")
    except PermissionError:
        print("permission denied (root process)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kill", type=int)
    ap.add_argument("--details", type=int)
    ap.add_argument("--stale", action="store_true")
    ap.add_argument("--stop-group")
    ap.add_argument("--restart-group")
    ap.add_argument("--faults", action="store_true")
    ap.add_argument("--fix")
    ap.add_argument("--action")
    ap.add_argument("--start", type=float)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--idle", type=int, default=0)
    ap.add_argument("--cpu", type=float, default=50, help="%% of one core, per process, while idle")
    ap.add_argument("--gpu", type=float, default=30)
    ap.add_argument("--temp", type=float, default=85)
    ap.add_argument("--hung-minutes", type=float, default=10)
    ap.add_argument("--no-notify", action="store_true")
    a = ap.parse_args()
    if a.stale:
        return stale()
    if a.stop_group:
        return stop_group(a.stop_group)
    if a.restart_group:
        return restart_group(a.restart_group)
    if a.faults:
        return print(json.dumps({"faults": find_faults(processes())}))
    if a.fix:
        return fix(a.fix, a.action)
    if a.details:
        return details(a.details, a.start)
    if a.kill:
        return kill(a.kill, a.start, a.force)

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(STATE) as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        now = time.time()
        if now - state.get("t", 0) < 30 and state.get("idle") == a.idle:
            hidden = acks()  # the cache predates any ✓ clicked since
            print(json.dumps({"issues": state.get("issues", []),
                              "faults": [f for f in state.get("faults", []) if f["key"] not in hidden]}))
            return

        uptime = float(read("/proc/uptime").split()[0])
        procs = processes()
        interval = now - state["t"] if 20 <= now - state.get("t", 0) <= 600 else 0
        issues = [with_prompt(i) for i in find_issues(a, procs, state.get("procs", {}), interval, uptime)]
        faults = find_faults(procs)

        notified = set(state.get("notified", []))
        if not a.no_notify:
            for i in issues:
                if i["key"] not in notified:
                    title = "Hung process" if i["kind"] == "hung" else "Heavy activity while you were away"
                    subprocess.run(["notify-send", "-u", "critical", "-a", "Idle watch", title, i["text"]])
            for f in faults:
                if f["urgent"] and f["key"] not in notified:
                    subprocess.run(["notify-send", "-u", "critical", "-a", "Idle watch", "System problem", f["title"]])

        with open(STATE, "w") as f:
            json.dump({"t": now, "idle": a.idle, "issues": issues, "faults": faults,
                       "notified": [i["key"] for i in issues + faults],
                       "procs": {pid: [p["start"], p["cpu"]] for pid, p in procs.items()}}, f)
        print(json.dumps({"issues": issues, "faults": faults}))


if __name__ == "__main__":
    main()
