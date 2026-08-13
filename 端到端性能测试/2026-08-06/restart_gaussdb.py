#!/usr/bin/env python3
"""Kill gaussdb, clear plugin cache, restart, verify."""
import os, shutil, subprocess, time

PG_PLUGIN = os.path.expanduser("~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/lib/postgresql/pg_plugin")
GAUSSHOME = os.path.expanduser("~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install")
GAUSSDB = f"{GAUSSHOME}/bin/gaussdb"
GSQL = f"{GAUSSHOME}/bin/gsql"

# 1. Kill
print("Stopping gaussdb...")
subprocess.run(["pkill", "-f", "gaussdb.*37000"], capture_output=True)
time.sleep(3)

# 2. Clear cache
if os.path.exists(PG_PLUGIN):
    bak = PG_PLUGIN + ".bak"
    if os.path.exists(bak):
        shutil.rmtree(bak)
    shutil.move(PG_PLUGIN, bak)
    print(f"Cache backed up: {bak}")
os.makedirs(PG_PLUGIN)

# 3. Start
print("Starting gaussdb...")
env = os.environ.copy()
env["GAUSSLOG"] = "/tmp"
# Source opengauss.env
with open(os.path.expanduser("~/iceberg-og/opengauss.env")) as f:
    for line in f:
        if line.startswith("export "):
            k, v = line[7:].strip().split("=", 1)
            v = v.strip('"')
            if "$" not in v:
                env[k] = v

subprocess.Popen(
    [GAUSSDB, "-D", os.path.expanduser("~/ogdata"), "-p", "37000"],
    env=env, stdout=open("/tmp/gaussdb.log", "w"), stderr=subprocess.STDOUT
)
time.sleep(8)

# 4. Verify
r = subprocess.run([GSQL, "-d", "postgres", "-p", "37000", "-h", "/tmp", "-c", "SELECT 1"],
                   capture_output=True, text=True, env=env)
print(r.stdout if r.returncode == 0 else f"FAILED: {r.stderr}")
