#!/usr/bin/env python3
"""HTTP Range 多线程并行下载，用于大文件加速。

Usage:
  python3 parallel_dl.py <url> <output_path> [threads]
  python3 parallel_dl.py https://example.com/big.bin ./big.bin 16
"""
import sys, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request

def dl_chunk(url, start, end, idx, out_path):
    req = urllib.request.Request(url, headers={'Range': f'bytes={start}-{end}'})
    resp = urllib.request.urlopen(req, timeout=300)
    buf = bytearray(1024 * 1024)
    with open(out_path, 'r+b') as f:
        f.seek(start)
        pos = start
        while True:
            n = resp.readinto(buf)
            if not n:
                break
            f.seek(pos)
            f.write(buf[:n])
            pos += n

def parallel_download(url, out_path, workers=8):
    req = urllib.request.Request(url, method='HEAD')
    resp = urllib.request.urlopen(req, timeout=30)
    total = int(resp.headers['Content-Length'])
    print(f'Size: {total/1024/1024:.0f} MiB, workers={workers}')

    # Pre-allocate file
    with open(out_path, 'wb') as f:
        f.truncate(total)

    chunk = (total + workers - 1) // workers
    start_time = time.time()
    done_count = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {}
        for i in range(workers):
            s = i * chunk
            e = min(s + chunk - 1, total - 1)
            if s < total:
                futs[pool.submit(dl_chunk, url, s, e, i, out_path)] = (i, s, e - s + 1)

        for fut in as_completed(futs):
            fut.result()  # may raise
            done_count += 1
            downloaded = done_count * chunk
            elapsed = max(time.time() - start_time, 0.001)
            speed = downloaded / elapsed / 1024 / 1024
            eta = (total - downloaded) / (speed * 1024 * 1024) / 60 if speed > 0 else 0
            print(f'  {done_count}/{workers}, {speed:.1f} MiB/s, ETA {eta:.0f}min')

    elapsed = time.time() - start_time
    size_mib = total / 1024 / 1024
    print(f'Done: {size_mib:.0f} MiB in {elapsed:.0f}s ({size_mib/elapsed:.1f} MiB/s)')

if __name__ == '__main__':
    url = sys.argv[1]
    out = sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    parallel_download(url, out, workers)
