#!/usr/bin/env python3
"""Telemetry start: capture static host facts and launch the sampler.

Writes into --dir:
  host.json        static facts about this runner + job start timestamp
  samples.jsonl    appended to by the detached sampler process
  sampler.pid      pid of the sampler, read by finalize.py
  sampler.log      sampler stderr, for debugging

Best-effort throughout: any individual fact that cannot be read is recorded
as null and the job continues.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def cpu_model():
    with open("/proc/cpuinfo") as f:
        for line in f:
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    # ARM /proc/cpuinfo has no "model name"; fall back to lscpu.
    out = subprocess.run(["lscpu"], capture_output=True, text=True, timeout=10).stdout
    for line in out.splitlines():
        if line.startswith("Model name:"):
            return line.split(":", 1)[1].strip()
    return None


def mem_total_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024.0, 1)
    return None


def os_pretty_name():
    with open("/etc/os-release") as f:
        for line in f:
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    return None


def virtualization():
    out = subprocess.run(
        ["systemd-detect-virt"], capture_output=True, text=True, timeout=10
    ).stdout.strip()
    return out or None


def dmi(field):
    with open(f"/sys/class/dmi/id/{field}") as f:
        return f.read().strip() or None


def collect_host_facts(runner_label):
    return {
        "cpu_model": safe(cpu_model),
        "cpu_count": safe(os.cpu_count),
        "mem_total_mb": safe(mem_total_mb),
        "kernel": safe(platform.release),
        "os": safe(os_pretty_name),
        "arch": safe(platform.machine),
        "virtualization": safe(virtualization),
        "dmi_sys_vendor": safe(lambda: dmi("sys_vendor")),
        "dmi_product_name": safe(lambda: dmi("product_name")),
        "runner_name": os.environ.get("RUNNER_NAME"),
        "runner_environment": os.environ.get("RUNNER_ENVIRONMENT"),
        "runner_os": os.environ.get("RUNNER_OS"),
        "runner_arch": os.environ.get("RUNNER_ARCH"),
        # Requested runs-on label, passed in by the workflow. Actual assigned
        # labels are not exposed as env vars, so this is the requested set.
        "requested_runs_on": runner_label or None,
        "image_os": os.environ.get("ImageOS"),
        "image_version": os.environ.get("ImageVersion"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--runner-label", default="")
    args = parser.parse_args()

    os.makedirs(args.dir, exist_ok=True)

    host = {
        "job_started_at_epoch": round(time.time(), 3),
        "sample_interval_seconds": args.interval,
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "github_repository": os.environ.get("GITHUB_REPOSITORY"),
        "github_job": os.environ.get("GITHUB_JOB"),
        "host": collect_host_facts(args.runner_label),
    }
    with open(os.path.join(args.dir, "host.json"), "w") as f:
        json.dump(host, f, indent=2)

    sampler = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sampler.py")
    samples_path = os.path.join(args.dir, "samples.jsonl")
    log = open(os.path.join(args.dir, "sampler.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, sampler, "--out", samples_path, "--interval", str(args.interval)],
        stdout=subprocess.DEVNULL,
        stderr=log,
        start_new_session=True,  # detach so the step can finish while it samples
    )
    with open(os.path.join(args.dir, "sampler.pid"), "w") as f:
        f.write(str(proc.pid))

    print(f"telemetry: sampler pid={proc.pid} interval={args.interval}s dir={args.dir}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"telemetry start failed (non-fatal): {exc}", file=sys.stderr)
        sys.exit(0)
