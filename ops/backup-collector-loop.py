"""5s cadence wrapper around backup-collector.py."""
import os, sys, time, runpy

CADENCE = float(os.environ.get("DAOBRAIN_OPS_CADENCE", "5"))
sys.path.insert(0, "/root/DaoBrain/ops")

while True:
    t0 = time.monotonic()
    try:
        runpy.run_path("/root/DaoBrain/ops/backup-collector.py", run_name="__main__")
    except Exception as e:
        sys.stderr.write("backup-collector error: %s: %s\n" % (type(e).__name__, str(e)[:200]))
    dt = time.monotonic() - t0
    time.sleep(max(0.5, CADENCE - dt))
