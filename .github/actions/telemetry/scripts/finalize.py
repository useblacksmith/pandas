#!/usr/bin/env python3
"""Telemetry stop: terminate the sampler and assemble telemetry.json.

Merges host facts, the full sample series, and derived aggregates into a
single telemetry.json inside --dir, which the workflow then uploads as the
`telemetry-<run_id>` artifact. Always writes *something* -- even if the
sampler never started, a stub with ok=false is produced so the artifact
explains itself instead of being silently absent.
"""

import argparse
import json
import os
import signal
import sys
import time

SCHEMA_VERSION = 1


def stop_sampler(pid_path):
    if not os.path.exists(pid_path):
        return "no pidfile"
    try:
        pid = int(open(pid_path).read().strip())
    except Exception:
        return "unreadable pidfile"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already exited"
    except Exception as exc:
        return f"sigterm failed: {exc}"
    # Give it a moment to flush its last sample and exit cleanly.
    for _ in range(10):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "stopped"
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    return "killed"


def load_samples(path):
    samples = []
    if not os.path.exists(path):
        return samples
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn final line from SIGKILL is expected
    return samples


def series(samples, key):
    return [s[key] for s in samples if s.get(key) is not None]


def counter_delta(samples, key):
    vals = series(samples, key)
    if len(vals) < 2:
        return None
    return max(vals[-1] - vals[0], 0)


def aggregates(samples):
    cpu = series(samples, "cpu_pct")
    mem = series(samples, "mem_used_mb")
    load = series(samples, "load1")
    return {
        "cpu_pct_mean": round(sum(cpu) / len(cpu), 1) if cpu else None,
        "cpu_pct_max": max(cpu) if cpu else None,
        "mem_used_mb_peak": max(mem) if mem else None,
        "load1_max": max(load) if load else None,
        "disk_read_bytes_total": counter_delta(samples, "disk_read_bytes"),
        "disk_write_bytes_total": counter_delta(samples, "disk_write_bytes"),
        "net_rx_bytes_total": counter_delta(samples, "net_rx_bytes"),
        "net_tx_bytes_total": counter_delta(samples, "net_tx_bytes"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    args = parser.parse_args()

    out_path = os.path.join(args.dir, "telemetry.json")
    os.makedirs(args.dir, exist_ok=True)

    sampler_status = stop_sampler(os.path.join(args.dir, "sampler.pid"))

    host = {}
    host_path = os.path.join(args.dir, "host.json")
    if os.path.exists(host_path):
        try:
            host = json.load(open(host_path))
        except Exception:
            host = {}

    samples = load_samples(os.path.join(args.dir, "samples.jsonl"))
    ended = round(time.time(), 3)
    started = host.get("job_started_at_epoch")

    telemetry = {
        "schema_version": SCHEMA_VERSION,
        "ok": bool(host) and len(samples) > 0,
        "sampler_status": sampler_status,
        "github_run_id": host.get("github_run_id") or os.environ.get("GITHUB_RUN_ID"),
        "github_repository": host.get("github_repository")
        or os.environ.get("GITHUB_REPOSITORY"),
        "host": host.get("host", {}),
        "sampling": {
            "interval_seconds": host.get("sample_interval_seconds"),
            "sample_count": len(samples),
            "started_at_epoch": started,
            "ended_at_epoch": ended,
            "duration_seconds": round(ended - started, 3) if started else None,
        },
        "aggregates": aggregates(samples),
        "samples": samples,
    }

    with open(out_path, "w") as f:
        json.dump(telemetry, f)

    agg = telemetry["aggregates"]
    print(
        f"telemetry: {len(samples)} samples, "
        f"cpu mean/max {agg['cpu_pct_mean']}/{agg['cpu_pct_max']}%, "
        f"peak mem {agg['mem_used_mb_peak']} MB -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"telemetry finalize failed (non-fatal): {exc}", file=sys.stderr)
        # Last-resort stub so the artifact upload still has a file.
        try:
            import argparse as _a  # already imported; keep flake-safe

            d = sys.argv[sys.argv.index("--dir") + 1]
            with open(os.path.join(d, "telemetry.json"), "w") as f:
                json.dump(
                    {"schema_version": SCHEMA_VERSION, "ok": False, "error": str(exc)}, f
                )
        except Exception:
            pass
        sys.exit(0)
