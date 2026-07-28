#!/usr/bin/env python3
"""Generate synthetic high-dim vector datasets in fbin format.

Two modes:
  gaussian  — Mixture of Gaussians (realistic clustering, IVF-friendly)
  uniform   — Uniform random (worst-case for IVF)

Usage:
  # 1024-dim, 10M base, 10K query, gaussian mixture
  python3 gen_synth.py --dim 1024 --n 10000000 --nq 10000 \
      --mode gaussian --seed 42 --out synth_1024d_10M

  # 2048-dim, 1M, uniform
  python3 gen_synth.py --dim 2048 --n 1000000 --mode uniform
"""
import argparse, os, struct, sys
import numpy as np


def gen_gaussian(dim, n, nq, n_clusters=200, seed=42):
    """Generate clustered data: n_clusters centers + Gaussian noise.
    Clusters are well-separated (centers spread across hypersphere surface).
    Query vectors sample from random clusters to test recall."""
    rng = np.random.default_rng(seed)

    # Cluster centers on the unit hypersphere surface
    centers = rng.normal(0, 1, (n_clusters, dim)).astype(np.float32)
    centers /= np.linalg.norm(centers, axis=1, keepdims=True) * np.sqrt(dim)

    # Data: assign each point to a random cluster, add noise
    base = np.zeros((n, dim), dtype=np.float32)
    cluster_size = n // n_clusters
    for c in range(n_clusters):
        start = c * cluster_size
        end = n if c == n_clusters - 1 else (c + 1) * cluster_size
        k = end - start
        noise = rng.normal(0, 0.5, (k, dim)).astype(np.float32)
        base[start:end] = centers[c] + noise

    # Queries: pick random clusters, sample near centers (small noise)
    q_clusters = rng.integers(0, n_clusters, nq)
    queries = np.zeros((nq, dim), dtype=np.float32)
    for i, c in enumerate(q_clusters):
        queries[i] = centers[c] + rng.normal(0, 0.1, dim).astype(np.float32)

    return base, queries


def gen_uniform(dim, n, nq, seed=42):
    """Uniform random data (no cluster structure)."""
    rng = np.random.default_rng(seed)
    base = rng.normal(0, 1, (n, dim)).astype(np.float32)
    queries = rng.normal(0, 1, (nq, dim)).astype(np.float32)
    return base, queries


def write_fbin(path, arr):
    n, dim = arr.shape
    with open(path, "wb") as f:
        f.write(struct.pack("<ii", n, dim))
        arr.tofile(f)
    size_mb = arr.nbytes / (1024 * 1024)
    print(f"  {path}: {n} × {dim} = {size_mb:.0f} MB")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dim", type=int, required=True)
    p.add_argument("--n", type=int, required=True,
                   help="Number of base vectors")
    p.add_argument("--nq", type=int, default=10000,
                   help="Number of query vectors (default: 10000)")
    p.add_argument("--mode", choices=["gaussian", "uniform"], default="gaussian")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default=None,
                   help="Output prefix (default: synth_{dim}d_{n})")
    args = p.parse_args()

    if args.out is None:
        n_label = f"{args.n // 1_000_000}M" if args.n >= 1_000_000 else str(args.n)
        args.out = f"synth_{args.dim}d_{n_label}"
    out_base = os.path.join(os.path.dirname(__file__), f"{args.out}_base.fbin")
    out_query = os.path.join(os.path.dirname(__file__), f"{args.out}_query.fbin")

    gen_fn = {"gaussian": gen_gaussian, "uniform": gen_uniform}[args.mode]
    print(f"Generating {args.dim}-dim, {args.n:,} base + {args.nq:,} query "
          f"({args.mode}, seed={args.seed})")
    base, queries = gen_fn(args.dim, args.n, args.nq, args.seed)
    write_fbin(out_base, base)
    write_fbin(out_query, queries)

    # Ground truth hint
    print(f"\nGround truth: run FullScan with these queries against the base table.")
    print(f"Import: python3 setup_fixed.py --input {out_base} "
          f"--namespace synth_ns --table {args.out} --chunk-size 500000")


if __name__ == "__main__":
    main()
