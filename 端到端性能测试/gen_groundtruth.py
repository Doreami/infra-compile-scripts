#!/usr/bin/env python3
"""生成 fbin 数据集的 ground truth（暴力 KNN）。

Usage:
  python3 gen_groundtruth.py --base synth2048v_10m_base.fbin \
      --query synth2048v_10m_query.fbin --k 100 --output synth2048v_10m_gt.bin
"""
import argparse, os, struct, sys, time
import numpy as np

def write_gt_bin(path, gt_matrix):
    """Write ground truth in big-ann format: [nq:uint32][k:uint32][ids:uint32*]. 0-based."""
    nq, k = gt_matrix.shape
    with open(path, 'wb') as f:
        f.write(struct.pack('<II', nq, k))
        gt_matrix.astype(np.uint32).tofile(f)
    print(f"  wrote: {path} ({os.path.getsize(path)/1024/1024:.1f} MB)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--base', required=True)
    p.add_argument('--query', required=True)
    p.add_argument('--k', type=int, default=100)
    p.add_argument('--output', required=True)
    p.add_argument('--chunk-size', type=int, default=10000,
                   help='Base rows per chunk (smaller = less memory). '
                        'Each chunk uses ~ nq × chunk × 4 bytes')
    args = p.parse_args()

    # Read query vectors
    qf = open(args.query, 'rb')
    nq = struct.unpack('<i', qf.read(4))[0]
    dim = struct.unpack('<i', qf.read(4))[0]
    queries = np.frombuffer(qf.read(nq * dim * 4), dtype=np.float32).reshape(nq, dim)
    qf.close()
    print(f"Queries: {nq} × {dim}")

    # Memory-map base
    base = np.memmap(args.base, dtype=np.float32, mode='r', offset=8)
    bf = open(args.base, 'rb')
    nb = struct.unpack('<i', bf.read(4))[0]
    rd = struct.unpack('<i', bf.read(4))[0]
    bf.close()
    assert rd == dim, f"dim mismatch: base={rd}, query={dim}"
    base = base[:nb * dim].reshape(nb, dim)
    print(f"Base: {nb} × {dim} ({base.nbytes/1024/1024/1024:.1f} GB)")

    # Top-K heap: [nq × k] indices and distances
    top_k_ids = np.zeros((nq, args.k), dtype=np.int32)
    top_k_dist = np.full((nq, args.k), np.inf, dtype=np.float32)

    t0 = time.time()
    for start in range(0, nb, args.chunk_size):
        end = min(start + args.chunk_size, nb)
        chunk = base[start:end]
        chunk_ids = np.arange(start, end, dtype=np.int32)

        # Compute distances: (chunk_size, nq) via (chunk, dim) @ (dim, nq)
        dists = chunk @ queries.T  # (chunk, nq)
        dists = -2.0 * dists  # -2 * dot
        # Add squared norms (optional — just dot product is fine for L2 ranking)
        # Actually we need proper L2: ||a-b||^2 = ||a||^2 + ||b||^2 - 2*a·b
        # For ranking, ||b||^2 is constant per query, ||a||^2 varies
        # We need full L2 for correctness
        base_norms = np.sum(chunk ** 2, axis=1, keepdims=True)  # (chunk, 1)
        query_norms = np.sum(queries ** 2, axis=1)  # (nq,)
        dists = base_norms + query_norms - 2.0 * chunk @ queries.T  # (chunk, nq)

        # For each query, merge top-K
        for q in range(nq):
            q_dists = dists[:, q]  # (chunk,)
            # Concatenate and sort
            all_ids = np.concatenate([top_k_ids[q], chunk_ids])
            all_dists = np.concatenate([top_k_dist[q], q_dists])
            order = np.argpartition(all_dists, args.k)[:args.k]
            top_k_ids[q] = all_ids[order]
            top_k_dist[q] = all_dists[order]

        elapsed = time.time() - t0
        pct = 100.0 * end / nb
        eta = elapsed / (end / nb) - elapsed if end > 0 else 0
        print(f"  {end}/{nb} ({pct:.1f}%), {elapsed:.0f}s elapsed, ETA {eta:.0f}s")

    # Sort final top-K
    for q in range(nq):
        order = np.argsort(top_k_dist[q])
        top_k_ids[q] = top_k_ids[q][order]
        top_k_dist[q] = top_k_dist[q][order]

    write_gt_bin(args.output, top_k_ids[:, :args.k])

    # Verify
    print(f"\nDone in {time.time()-t0:.0f}s")
    print(f"Sample Q0 top-10: {top_k_ids[0, :10].tolist()}")


if __name__ == '__main__':
    main()
