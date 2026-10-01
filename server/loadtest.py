"""
Phase 12 - relay load tester.

Opens many concurrent host/viewer pairs against a relay and measures what
an operator needs to size it: how many sessions actually establish, how
long CONNECT takes, per-round-trip latency once paired, aggregate
throughput, and (for a relay this script launches itself, on Linux) the
relay process's threads, resident memory and CPU.

Each simulated session does what a real one does at the relay's level:
  host    REGISTER <name>
  viewer  CONNECT <name>  -> waits for OK          (connect latency)
  then N rounds of:  viewer -> host  one "frame" of --frame-kb KiB
                     host   -> viewer a small "input ack" of --ack-bytes
  (round-trip latency = frame sent until ack received)
It sends opaque bytes, not TLS or the real protocol - the relay can't tell
the difference, so this measures the relay, not the desktop apps.

Examples:
    python3 loadtest.py --sessions 500                     # launches ./relay.py itself
    python3 loadtest.py --sessions 2000 --ramp 10 --rounds 20
    python3 loadtest.py --relay-host relay.example.com --relay-port 6000 --sessions 300
    python3 loadtest.py --relay-script ../baseline_relay.py --sessions 500   # compare implementations

Read the numbers for what they are: a generator and a relay sharing one
machine's loopback measure the relay's software limits (threads, locks,
buffers) - not your network, not TLS, not real screen-content bitrates. Run
the generator from a separate machine, against the deployed relay, before
trusting capacity numbers for production.
"""

import argparse
import asyncio
import json
import os
import resource
import socket
import statistics
import subprocess
import sys
import time


def raise_fd_limit() -> int:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = hard if hard != resource.RLIM_INFINITY else 1_048_576
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError):
        target = soft
    return target


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    k = min(len(values) - 1, max(0, int(round(p / 100 * (len(values) - 1)))))
    return values[k]


# --- /proc sampling of a relay we launched ---------------------------------

def proc_sample(pid: int):
    try:
        with open(f"/proc/{pid}/status") as f:
            status = f.read()
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read().rsplit(")", 1)[1].split()
        threads = int(next(l for l in status.splitlines() if l.startswith("Threads:")).split()[1])
        rss_kb = int(next(l for l in status.splitlines() if l.startswith("VmRSS:")).split()[1])
        cpu_ticks = int(stat[11]) + int(stat[12])           # utime + stime
        return {"threads": threads, "rss_mb": rss_kb / 1024,
                "cpu_s": cpu_ticks / os.sysconf("SC_CLK_TCK")}
    except (OSError, StopIteration, IndexError, ValueError):
        return None


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def launch_relay(script: str, extra_args: list):
    port = free_port()
    args = [sys.executable, script, "--port", str(port)] + extra_args
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            preexec_fn=lambda: raise_fd_limit())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return proc, port
        except OSError:
            if proc.poll() is not None:
                raise SystemExit(f"relay exited early: {proc.stderr.read().decode()[:500]}")
            time.sleep(0.1)
    proc.kill()
    raise SystemExit("relay did not start listening in 5s")


# --- one simulated session -------------------------------------------------

async def one_session(idx: int, host: str, port: int, args, results: dict) -> None:
    name = f"lt-{os.getpid()}-{idx}"
    frame = b"F" * (args.frame_kb * 1024)
    ack = b"A" * args.ack_bytes
    hw = vw = None
    try:
        # host registers
        hr, hw = await asyncio.wait_for(asyncio.open_connection(host, port), args.timeout)
        hw.write(f"REGISTER {name}\n".encode())
        await hw.drain()

        # viewer connects, measure time to OK
        t0 = time.perf_counter()
        vr, vw = await asyncio.wait_for(asyncio.open_connection(host, port), args.timeout)
        vw.write(f"CONNECT {name}\n".encode())
        await vw.drain()
        reply = await asyncio.wait_for(vr.readline(), args.timeout)
        if reply.strip() != b"OK":
            results["fail"].append(f"connect:{reply.strip().decode(errors='replace') or 'closed'}")
            return
        results["connect_ms"].append((time.perf_counter() - t0) * 1000)
        results["established"] += 1
        results["live"] += 1
        results["peak_live"] = max(results["peak_live"], results["live"])

        try:
            for _ in range(args.rounds):
                t1 = time.perf_counter()
                vw.write(frame)
                await vw.drain()
                await asyncio.wait_for(hr.readexactly(len(frame)), args.timeout)
                hw.write(ack)
                await hw.drain()
                await asyncio.wait_for(vr.readexactly(len(ack)), args.timeout)
                results["rtt_ms"].append((time.perf_counter() - t1) * 1000)
                results["bytes"] += len(frame) + len(ack)
                if args.think_ms:
                    await asyncio.sleep(args.think_ms / 1000)
            results["completed"] += 1
        finally:
            results["live"] -= 1
    except asyncio.TimeoutError:
        results["fail"].append("timeout")
    except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
        results["fail"].append(f"{type(e).__name__}")
    finally:
        for w in (hw, vw):
            if w is not None:
                w.close()


async def run_load(host: str, port: int, args, relay_pid=None) -> dict:
    results = {"established": 0, "completed": 0, "live": 0, "peak_live": 0, "bytes": 0,
               "connect_ms": [], "rtt_ms": [], "fail": []}
    peak = {"threads": 0, "rss_mb": 0.0}
    cpu_start = proc_sample(relay_pid) if relay_pid else None

    async def sampler():
        while True:
            s = proc_sample(relay_pid) if relay_pid else None
            if s:
                peak["threads"] = max(peak["threads"], s["threads"])
                peak["rss_mb"] = max(peak["rss_mb"], s["rss_mb"])
            await asyncio.sleep(0.25)

    sampler_task = asyncio.create_task(sampler())
    start = time.perf_counter()
    tasks = []
    gap = args.ramp / args.sessions if args.ramp else 0
    for i in range(args.sessions):
        tasks.append(asyncio.create_task(one_session(i, host, port, args, results)))
        if gap:
            await asyncio.sleep(gap)
        elif i % 200 == 199:
            await asyncio.sleep(0)        # let the loop breathe when not ramping
    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - start
    sampler_task.cancel()
    cpu_end = proc_sample(relay_pid) if relay_pid else None

    out = {
        "sessions_requested": args.sessions,
        "sessions_established": results["established"],
        "sessions_completed": results["completed"],
        "peak_concurrent_sessions": results["peak_live"],
        "failures": {k: results["fail"].count(k) for k in sorted(set(results["fail"]))},
        "connect_ms": {"p50": pct(results["connect_ms"], 50), "p95": pct(results["connect_ms"], 95),
                       "p99": pct(results["connect_ms"], 99), "max": max(results["connect_ms"], default=None)},
        "round_trip_ms": {"p50": pct(results["rtt_ms"], 50), "p95": pct(results["rtt_ms"], 95),
                          "p99": pct(results["rtt_ms"], 99), "max": max(results["rtt_ms"], default=None)},
        "elapsed_s": round(elapsed, 2),
        "throughput_MB_per_s": round(results["bytes"] / 1e6 / elapsed, 2) if elapsed else None,
        "total_MB": round(results["bytes"] / 1e6, 1),
    }
    if cpu_start and cpu_end:
        out["relay_process"] = {"peak_threads": peak["threads"], "peak_rss_mb": round(peak["rss_mb"], 1),
                                "cpu_seconds_used": round(cpu_end["cpu_s"] - cpu_start["cpu_s"], 2),
                                "avg_cpu_percent_of_one_core": round(
                                    100 * (cpu_end["cpu_s"] - cpu_start["cpu_s"]) / elapsed, 1)}
    for section in ("connect_ms", "round_trip_ms"):
        out[section] = {k: (round(v, 1) if v is not None else None) for k, v in out[section].items()}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Relay load tester (Phase 12)")
    ap.add_argument("--relay-host")
    ap.add_argument("--relay-port", type=int, default=6000)
    ap.add_argument("--relay-script", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay.py"),
                    help="relay to launch when --relay-host isn't given (default: ./relay.py)")
    ap.add_argument("--relay-arg", action="append", default=[], help="extra arg for the launched relay")
    ap.add_argument("--sessions", type=int, default=200)
    ap.add_argument("--ramp", type=float, default=0.0, help="spread session starts over this many seconds")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--frame-kb", type=int, default=32, help="size of each viewer->host 'frame'")
    ap.add_argument("--ack-bytes", type=int, default=256)
    ap.add_argument("--think-ms", type=float, default=0.0, help="pause between rounds (real screens are ~66ms/frame at 15fps)")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--json", action="store_true", help="print only the JSON result")
    args = ap.parse_args()

    limit = raise_fd_limit()
    needed = args.sessions * 2 + 64
    if needed > limit:
        print(f"warning: need ~{needed} file descriptors for the generator, limit is {limit}; "
              f"expect failures (raise `ulimit -n`)", file=sys.stderr)

    proc = None
    if args.relay_host:
        host, port, pid = args.relay_host, args.relay_port, None
    else:
        proc, port = launch_relay(args.relay_script, args.relay_arg)
        host, pid = "127.0.0.1", proc.pid
        if not args.json:
            print(f"launched {os.path.basename(args.relay_script)} (pid {pid}) on 127.0.0.1:{port}; "
                  f"fd limit {limit}")
    try:
        result = asyncio.run(run_load(host, port, args, pid))
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
    print(json.dumps(result, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
