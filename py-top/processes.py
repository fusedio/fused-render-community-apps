"""runPython target for PyTop (index.html): system stats + process list via bash, and kill.

Stdlib only. Actions:
  main(action="stats")            -> {"system": {...}, "processes": [...]}
  main(action="kill", pid="123")  -> {"ok": bool, ...}   (force="1" for SIGKILL)
"""
import os
import signal
import subprocess


def bash(cmd: str) -> str:
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=10).stdout


def get_processes():
    out = bash("ps axo pid,ppid,user,%cpu,%mem,rss,state,etime,command")
    procs = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        pid, ppid, user, cpu, mem, rss, state, etime, command = parts
        try:
            procs.append({
                "pid": int(pid), "ppid": int(ppid), "user": user,
                "cpu": float(cpu), "mem": float(mem), "rss_kb": int(rss),
                "state": state, "etime": etime, "command": command,
            })
        except ValueError:
            continue
    return procs


def get_system():
    ncpu = int(bash("sysctl -n hw.ncpu").strip() or 1)
    memsize = int(bash("sysctl -n hw.memsize").strip() or 0)
    loadavg = bash("sysctl -n vm.loadavg").strip().strip("{} ").split()[:3]

    cpu_line = bash("top -l 1 -n 0 | grep 'CPU usage'")
    cpu = {"user": 0.0, "sys": 0.0, "idle": 100.0}
    for tok in cpu_line.replace("CPU usage:", "").split(","):
        tok = tok.strip()
        for key in cpu:
            if tok.endswith(key):
                try:
                    cpu[key] = float(tok.split("%")[0])
                except ValueError:
                    pass

    vm = {}
    for line in bash("vm_stat").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip().rstrip(".")
            if v.isdigit():
                vm[k.strip()] = int(v)
    try:
        page = int(bash("pagesize").strip())
    except ValueError:
        page = 16384
    free = (vm.get("Pages free", 0) + vm.get("Pages inactive", 0)) * page
    used = memsize - free if memsize else 0

    return {
        "ncpu": ncpu,
        "load": [float(x) for x in loadavg] if loadavg else [],
        "cpu": cpu,
        "mem_total": memsize,
        "mem_used": used,
        "hostname": bash("hostname").strip(),
    }


def kill_process(pid: int, force: bool):
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, sig)
        return {"ok": True, "pid": pid, "signal": sig.name}
    except ProcessLookupError:
        return {"ok": False, "error": "no such process"}
    except PermissionError:
        return {"ok": False, "error": "permission denied (not your process)"}


def main(action: str = "stats", pid: str = "", force: str = "0") -> dict:
    if action == "kill":
        try:
            return kill_process(int(pid), force == "1")
        except ValueError:
            return {"ok": False, "error": "invalid pid"}
    if action == "killmany":
        results = []
        for p in pid.split(","):
            p = p.strip()
            if not p:
                continue
            try:
                results.append(kill_process(int(p), force == "1"))
            except ValueError:
                results.append({"ok": False, "pid": p, "error": "invalid pid"})
        killed = sum(1 for r in results if r.get("ok"))
        return {"ok": killed > 0, "killed": killed, "total": len(results), "results": results}
    return {"system": get_system(), "processes": get_processes()}
