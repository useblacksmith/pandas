#!/usr/bin/env python3
"""Background resource sampler for benchmark telemetry.

Runs as a detached process for the lifetime of a benchmark job and appends
one JSON line per sample to --out. All metrics are read from /proc (Linux
only, which covers every benchmark environment) so that no package install
is needed inside a measured run. See DECISIONS.md #2.

Per sample:
  t                 epoch seconds (float)
  cpu_pct           overall CPU utilization % since the previous sample
  mem_used_mb       MemTotal - MemAvailable, in MB
  disk_read_bytes   cumulative bytes read across whole physical devices
  disk_write_bytes  cumulative bytes written
  net_rx_bytes      cumulative bytes received (all interfaces except lo)
  net_tx_bytes      cumulative bytes sent
  load1             1-minute load average

Disk/network counters are cumulative since boot; the finalizer computes
totals as (last - first). The sampler must never crash the job: every read
is best-effort and any unexpected error terminates the sampler silently,
never the workload.
"""

import argparse
import json
import os
import re
import signal
import sys
import time

# Whole physical block devices only -- excludes partitions (sda1, nvme0n1p1),
# loop devices, device-mapper, and ramdisks so bytes are not double counted.
_WHOLE_DEVICE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|xvd[a-z]+|vd[a-z]+)$")

_SECTOR_BYTES = 512  # /proc/diskstats sector counts are always 512-byte units

_stop = False


def _handle_term(signum, frame):
    global _stop
    _stop = True


def read_cpu_times():
    """Return (busy, total) jiffies from the aggregate cpu line of /proc/stat."""
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu "):
                fields = [int(x) for x in line.split()[1:]]
                # user nice system idle iowait irq softirq steal [guest guest_nice]
                idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
                total = sum(fields[:8])
                return total - idle, total
    return None, None


def read_mem_used_mb():
    total_kb = avail_kb = None
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
            if total_kb is not None and avail_kb is not None:
                break
    if total_kb is None or avail_kb is None:
        return None
    return round((total_kb - avail_kb) / 1024.0, 1)


def read_disk_bytes():
    read_b = write_b = 0
    found = False
    with open("/proc/diskstats") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 10:
                continue
            name = parts[2]
            if not _WHOLE_DEVICE.match(name):
                continue
            found = True
            read_b += int(parts[5]) * _SECTOR_BYTES
            write_b += int(parts[9]) * _SECTOR_BYTES
    if not found:
        return None, None
    return read_b, write_b


def read_net_bytes():
    rx = tx = 0
    found = False
    with open("/proc/net/dev") as f:
        for line in f:
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            name = name.strip()
            if name == "lo":
                continue
            fields = rest.split()
            found = True
            rx += int(fields[0])
            tx += int(fields[8])
    if not found:
        return None, None
    return rx, tx


def read_load1():
    with open("/proc/loadavg") as f:
        return float(f.read().split()[0])


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument(
        "--max-duration",
        type=float,
        default=6 * 3600,
        help="hard stop in seconds, in case the stop step never runs "
        "(matters on persistent self-hosted runners)",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    started = time.time()
    prev_busy, prev_total = safe(read_cpu_times, (None, None))

    with open(args.out, "a", buffering=1) as out:
        while not _stop and (time.time() - started) < args.max_duration:
            time.sleep(args.interval)
            if _stop:
                break

            cpu_pct = None
            busy, total = safe(read_cpu_times, (None, None))
            if None not in (busy, total, prev_busy, prev_total) and total > prev_total:
                cpu_pct = round(100.0 * (busy - prev_busy) / (total - prev_total), 1)
            prev_busy, prev_total = busy, total

            disk_read, disk_write = safe(read_disk_bytes, (None, None))
            net_rx, net_tx = safe(read_net_bytes, (None, None))

            sample = {
                "t": round(time.time(), 3),
                "cpu_pct": cpu_pct,
                "mem_used_mb": safe(read_mem_used_mb),
                "disk_read_bytes": disk_read,
                "disk_write_bytes": disk_write,
                "net_rx_bytes": net_rx,
                "net_tx_bytes": net_tx,
                "load1": safe(read_load1),
            }
            out.write(json.dumps(sample) + "\n")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # A telemetry failure must never surface as a job failure.
        sys.exit(0)
