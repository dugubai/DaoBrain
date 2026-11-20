"""5s cadence wrapper around entry-collector.py — keeps SSH surface minimal for dashboard polling."""
import os, sys, time, runpy

sys.path.insert(0, "/root/DaoBrain/ops")
CADENCE = float(os.environ.get("DAOBRAIN_OPS_CADENCE", "5"))

while True:
    t0 = time.monotonic()
    try:
        runpy.run_path("/root/DaoBrain/ops/entry-collector.py", run_name="__main__")
    except Exception as e:
        sys.stderr.write("collector error: %s: %s\n" % (type(e).__name__, str(e)[:200]))
    dt = time.monotonic() - t0
    time.sleep(max(0.5, CADENCE - dt))
