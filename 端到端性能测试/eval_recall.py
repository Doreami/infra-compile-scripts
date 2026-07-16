#!/usr/bin/env python3
"""
Evaluate Recall@K for vector search results against SIFT1M ground truth.

Usage:
    python3 eval_recall.py --ground-truth path/to/sift_groundtruth.parquet \
                           --results path/to/query_results.csv

results.csv format: qid,top1_id,top2_id,...,topK_id (one row per query, comma-separated)
"""
import argparse
import numpy as np
import pyarrow.parquet as pq


def compute_recall(gt: np.ndarray, results: np.ndarray) -> float:
    """Compute Recall@K. gt shape: (n_queries, K), results shape: (n_queries, K)."""
    hits = 0
    total = 0
    for i in range(len(gt)):
        gt_set = set(gt[i])
        res_set = set(results[i])
        hits += len(gt_set & res_set)
        total += len(gt_set)
    return hits / total if total > 0 else 0.0


def main():
    parser = argparse.ArgumentParser(description="Compute SIFT1M Recall@K")
    parser.add_argument("--ground-truth", required=True, help="Ground truth parquet")
    parser.add_argument("--results", required=True, help="Query results CSV")
    parser.add_argument("--K", type=int, nargs="+", default=[1, 10, 100])
    args = parser.parse_args()

    # Load ground truth
    gt_table = pq.read_table(args.ground_truth)
    gt_array = np.array([row.as_py() for row in gt_table["neighbors"]], dtype=np.int32)
    nq, gt_k = gt_array.shape
    print(f"Ground truth: {nq} queries × top-{gt_k}")

    # Load results
    results = np.loadtxt(args.results, delimiter=",", dtype=np.int32)
    nqr, res_k = results.shape
    print(f"Results: {nqr} queries × top-{res_k}")

    assert nq == nqr, f"Query count mismatch: gt={nq}, results={nqr}"

    for k in args.K:
        k_actual = min(k, res_k, gt_k)
        recall = compute_recall(gt_array[:, :k_actual], results[:, :k_actual])
        print(f"Recall@{k:>3d}: {recall:.4f}")


if __name__ == "__main__":
    main()
