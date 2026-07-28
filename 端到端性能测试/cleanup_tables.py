#!/usr/bin/env python3
"""Clean up GIST/SIFT tables before re-running tests."""
import os, subprocess, sys

gsql = os.path.expanduser(os.environ.get("GAUSSHOME", "gaussdb")) + "/bin/gsql"
if not os.path.exists(gsql):
    gsql = "gsql"

warehouse = os.path.expanduser(
    os.environ.get("ICEBERG_WAREHOUSE", "/data/xl/warehouse").replace("file://", ""))

for ns in ["gist_ns", "sift_ns"]:
    for tbl in ["gist1m", "sift1m"]:
        subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-c",
                        f"DROP FOREIGN TABLE IF EXISTS {ns}.{tbl};"],
                       capture_output=True, timeout=15)
        r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-t", "-A", "-c",
                            f"SELECT iceberg_catalog.unregister_table('{ns}', '{tbl}');"],
                           capture_output=True, text=True, timeout=15)
        print(f"  unregister {ns}.{tbl}: {r.stdout.strip()} {r.stderr.strip()}")
    path = os.path.join(warehouse, ns)
    if os.path.exists(path):
        import shutil
        shutil.rmtree(path)
        print(f"  cleaned: {path}")

print("Done")
