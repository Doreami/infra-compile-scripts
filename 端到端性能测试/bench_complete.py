#!/usr/bin/env python3
"""完整测试：IVF+FullScan, K/DOP/Recall。

Usage:
  python3 bench_complete.py --all
  python3 bench_complete.py --dataset sift
  python3 bench_complete.py --dataset deep --skip-fullscan --skip-recall
  python3 bench_complete.py --dataset gist --k 10,100 --dop 1,8 --rounds 2
  python3 bench_complete.py --namespace my_ns --table my_tbl --dim 128 --query-file ~/q.fbin
"""
import argparse, subprocess, os, struct, json, sys
from datetime import datetime

GSQL = '/home/xl/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql'

CFG = {
    'sift': {
        'tbl': 'sift_ns.sift1m', 'dim': 128, 'rows': 1_000_000,
        'qf': 'sift_query.fvecs', 'fmt': 'fvecs',
    },
    'gist': {
        'tbl': 'gist_ns.gist1m', 'dim': 960, 'rows': 1_000_000,
        'qf': 'gist-960-euclidean.hdf5', 'fmt': 'hdf5', 'qk': 'test',
    },
    'deep': {
        'tbl': 'deep_ns.deep1b', 'dim': 96, 'rows': 1_000_000_000,
        'qf': 'query.public.10K.fbin', 'fmt': 'fbin',
        'fbin_dir': 'big-ann-benchmarks/data/deep1b',
    },
    'synth': {
        'tbl': 'synth_ns.synth2048_10m', 'dim': 2048, 'rows': 10_000_000,
        'qf': 'synth2048_10M_query.fbin', 'fmt': 'fbin',
    },
}


def load_qv(cfg, idx, query_file=None):
    if query_file:
        path = os.path.expanduser(query_file)
        fmt = 'fbin'
    else:
        d = './测试文件/'
        if 'fbin_dir' in cfg:
            d = '~/' + cfg['fbin_dir'] + '/'
        path = os.path.expanduser(d + cfg.get('qf', ''))
        fmt = cfg.get('fmt', 'fbin')

    if fmt == 'fvecs':
        with open(path, 'rb') as f:
            data = f.read()
        dim = struct.unpack_from('<i', data, 0)[0]
        off = 4 + dim * 4 + idx * (4 + dim * 4)
        return '[' + ','.join(str(struct.unpack_from('<f', data, off + i * 4)[0]) for i in range(dim)) + ']'
    elif fmt == 'hdf5':
        import h5py
        with h5py.File(path, 'r') as f:
            return '[' + ','.join(str(v) for v in f[cfg['qk']][idx].tolist()) + ']'
    else:
        f = open(path, 'rb')
        nvecs = struct.unpack('<i', f.read(4))[0]
        dim = struct.unpack('<i', f.read(4))[0]
        off = 8 + idx * dim * 4
        f.seek(off)
        vec = struct.unpack('<%df' % dim, f.read(dim * 4))
        f.close()
        return '[' + ','.join(str(v) for v in vec) + ']'


def gsql(sql, timeout=900):
    try:
        r = subprocess.run([GSQL, '-d', 'postgres', '-p', '37000', '-c', sql],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return '', 'timeout'


def gsql_raw(sql, timeout=30):
    """Run SQL with -t -A for clean output (no headers/formatting)."""
    try:
        r = subprocess.run([GSQL, '-d', 'postgres', '-p', '37000', '-t', '-A', '-c', sql],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr
    except subprocess.TimeoutExpired:
        return '', 'timeout'


def parse_ms(out):
    for line in out.split('\n'):
        if 'Total runtime' in line:
            return float(line.split(':')[-1].strip().replace(' ms', ''))
    return None


def load_gt(path, q_idx=0):
    """加载官方 GT 文件。返回 (K, [id+1, ...]) 或 (0, [])。"""
    if not path or not os.path.exists(os.path.expanduser(path)):
        return 0, []
    path = os.path.expanduser(path)
    with open(path, 'rb') as f:
        header = f.read(4)
    if path.endswith('.ivecs'):
        # 格式：[K:int32][ids:int32*K] per query, 0-based
        with open(path, 'rb') as f:
            K = struct.unpack('<i', f.read(4))[0]
            # Skip to query q_idx
            f.seek(4 + q_idx * (4 + K * 4))
            f.read(4)  # skip K
            ids = struct.unpack('<%dI' % K, f.read(K * 4))
        return K, [x + 1 for x in ids]  # 0-based → 1-based
    else:
        # .bin 格式：[nq:uint32][K:uint32][ids:uint*nq*K], 0-based
        with open(path, 'rb') as f:
            nq = struct.unpack('<I', f.read(4))[0]
            K = struct.unpack('<I', f.read(4))[0]
            # DEEP 的 GT 文件是 uint64
            is_u64 = 'deep' in os.path.basename(path).lower()
            fmt = '<%dQ' if is_u64 else '<%dI'
            id_bytes = 8 if is_u64 else 4
            all_ids = struct.unpack(fmt % (nq * K), f.read(nq * K * id_bytes))
        start = q_idx * K
        if is_u64:
            return K, [(all_ids[i] & 0xFFFFFFFF) + 1 for i in range(start, start + K)]
        else:
            return K, [x + 1 for x in all_ids[start:start + K]]


def parse_ids(out):
    ids = []
    for line in out.split('\n'):
        line = line.strip()
        if not line or 'rows' in line.lower() or 'Total runtime' in line:
            continue
        if 'QUERY PLAN' in line or 'Scan' in line or 'Index' in line:
            continue
        try:
            ids.append(int(line))
        except:
            continue
    return ids


def resolve_tbl(args, cfg):
    if args.table:
        tbl = args.table
    else:
        tbl = cfg.get('tbl', '')
    if args.namespace:
        parts = tbl.split('.', 1)
        tbl = args.namespace + '.' + (parts[1] if len(parts) > 1 else parts[0])
    return tbl


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='完整 ANN 性能测试')
    p.add_argument('--dataset', choices=['sift', 'gist', 'deep', 'synth'])
    p.add_argument('--all', action='store_true', help='测试全部四表')
    p.add_argument('--k', default='10,100,1000,10000')
    p.add_argument('--dop', default='1,2,4,8')
    p.add_argument('--skip-ivf', action='store_true')
    p.add_argument('--skip-fullscan', action='store_true')
    p.add_argument('--skip-recall', action='store_true')
    p.add_argument('--rounds', type=int, default=3)
    p.add_argument('--warmup', type=int, default=1)
    p.add_argument('--output', default='/tmp/bench_complete_results.json')
    p.add_argument('--query-idx', type=int, default=0)
    p.add_argument('--namespace', default=None, help='命名空间（--dataset 可省略）')
    p.add_argument('--table', default=None, help='表名（--dataset 可省略）')
    p.add_argument('--dim', type=int, default=None, help='向量维度（--dataset 可省略）')
    p.add_argument('--query-file', default=None, help='Query 文件路径（--dataset 可省略）')
    p.add_argument('--ground-truth', default=None, help='官方 GT 文件 (.ivecs 或 .bin)')
    p.add_argument('--tables', default=None, nargs='+', action='append',
                   help='自定义表列表，每组3-5个参数: NS TBL DIM [QUERY_FILE] [GT_FILE] (可重复多次)')
    args = p.parse_args()

    K_V = [int(x.strip()) for x in args.k.split(',')]
    DOP_V = [int(x.strip()) for x in args.dop.split(',')]

    # ── 解析测试目标列表 ──
    # targets: [(label, tbl, dim, qf_path, fmt, qk, gt_path)]
    targets = []

    if args.tables:
        for group in args.tables:
            if len(group) < 3 or len(group) > 5:
                p.error('--tables 每组需 3-5 个参数: NS TBL DIM [QUERY_FILE|GT_FILE] [GT_FILE]')
            ns, tbl, dim = group[0], group[1], int(group[2])
            qf, gt = '', args.ground_truth
            if len(group) == 4:
                # 第4个参数：.ivecs/.bin/含groundtruth → GT, 否则 → query file
                v = group[3]
                if v and (v.endswith('.ivecs') or v.endswith('.bin') or 'groundtruth' in v.lower() or 'gt' in v.lower()):
                    gt = v
                else:
                    qf = v
            elif len(group) == 5:
                qf, gt = group[3], group[4] if group[4] else args.ground_truth
            targets.append(('%s.%s' % (ns, tbl), '%s.%s' % (ns, tbl), dim, qf, 'fbin', None, gt))

    if args.all:
        for ds in ['sift', 'gist', 'deep', 'synth']:
            c = CFG[ds]
            targets.append((ds, resolve_tbl(args, c), c['dim'],
                           c.get('qf', ''), c.get('fmt', 'fbin'), c.get('qk'), args.ground_truth))
    elif args.dataset:
        c = CFG[args.dataset]
        targets.append((args.dataset, resolve_tbl(args, c), args.dim or c['dim'],
                       args.query_file or c.get('qf', ''), c.get('fmt', 'fbin'),
                       c.get('qk'), args.ground_truth))
    elif args.namespace and args.table and args.dim:
        targets.append(('custom', resolve_tbl(args, {}), args.dim,
                       args.query_file or '', 'fbin', None, args.ground_truth))
    elif not args.tables:
        p.error('需要 --dataset/--all/--tables 或 --namespace/--table/--dim')

    ALL_RESULTS = {}

    for label, tbl, dim, qf_path, fmt, qk, gt_path in targets:
        cfg = {'qf': qf_path, 'fmt': fmt, 'qk': qk}
        gt_K, gt_ids = load_gt(gt_path, args.query_idx) if gt_path else (0, [])

        print("\n" + "=" * 60)
        print("  %s (%d-dim)  %s  %s" % (label, dim, tbl,
                                          datetime.now().strftime('%H:%M:%S')))
        print("=" * 60)

        if args.query_file or cfg.get('qf'):
            qv = load_qv(cfg, args.query_idx, args.query_file)
            qv_src = 'file'
        else:
            # Fallback: 取表内 id=1 的向量
            if gt_path:
                p.error('指定了 GT 文件但未指定 query file，GT 必须匹配 query 文件向量')
            qv, _ = gsql_raw("SELECT vec FROM %s WHERE id=1;" % tbl, timeout=30)
            qv_src = 'table(id=1)'
        print("  QV len=%d (source: %s)" % (len(qv), qv_src))

        results = {}

        # ── Ground truth ──
        fs_truth = {}
        if not args.skip_recall:
            if gt_ids:
                print("  [Ground Truth] Official GT (K=%d)" % gt_K)
                for k in K_V:
                    if k <= gt_K:
                        fs_truth[k] = gt_ids[:k]
            else:
                max_k = max(K_V)
                print("  [Ground Truth] FullScan K=%d (no official GT)..." % max_k)
                sql = ("SET query_dop = 1; SET enable_vectorsearch = off; "
                       "SELECT id FROM %s ORDER BY vec <-> '%s'::vector LIMIT %d;" % (tbl, qv, max_k))
                out, err = gsql(sql, timeout=1800)
                fs_ids = parse_ids(out)
                if fs_ids:
                    print("  Got %d IDs" % len(fs_ids))
                    for k in K_V:
                        fs_truth[k] = fs_ids[:k]
                else:
                    print("  FAILED: %s" % err[:100])

        # ── IVF ──
        if not args.skip_ivf:
            print("  [IVF]")
            for k in K_V:
                for dop in DOP_V:
                    label = "IVF_K%d_DOP%d" % (k, dop)
                    setup = "SET query_dop = %d; SET enable_vectorsearch = on;" % dop
                    sql = "%s EXPLAIN ANALYZE SELECT id FROM %s ORDER BY vec <-> '%s'::vector LIMIT %d;" % (
                        setup, tbl, qv, k)

                    for _ in range(args.warmup):
                        gsql(sql, timeout=900)

                    times = []
                    ivf_ids = None
                    for i in range(args.rounds):
                        out, err = gsql(sql, timeout=900)
                        t = parse_ms(out)
                        if t is not None:
                            times.append(t)
                        if i == 0:
                            sql2 = "%s SELECT id FROM %s ORDER BY vec <-> '%s'::vector LIMIT %d;" % (
                                setup, tbl, qv, k)
                            out2, _ = gsql(sql2, timeout=900)
                            ivf_ids = parse_ids(out2)

                    if len(times) >= args.rounds:
                        median = sorted(times)[len(times) // 2]
                        avg = sum(times) / len(times)
                        recall = None
                        if k in fs_truth and ivf_ids:
                            recall = len(set(fs_truth[k]) & set(ivf_ids[:k])) / k
                        results[label] = {'median': median, 'avg': avg, 'recall': recall}
                        r_str = " recall@%d=%.3f" % (k, recall) if recall is not None else ""
                        raw = ','.join('%d' % int(t) for t in times)
                        print("  %-20s median=%8.0fms  avg=%8.0fms  [%s]%s" % (label, median, avg, raw, r_str))
                    else:
                        results[label] = {'median': None}
                        print("  %-20s FAILED" % label)

        # ── FullScan ──
        if not args.skip_fullscan:
            print("  [FullScan]")
            for k in K_V:
                for dop in DOP_V:
                    label = "FS_K%d_DOP%d" % (k, dop)
                    setup = "SET query_dop = %d; SET enable_vectorsearch = off;" % dop
                    sql = "%s EXPLAIN ANALYZE SELECT id FROM %s ORDER BY vec <-> '%s'::vector LIMIT %d;" % (
                        setup, tbl, qv, k)

                    for _ in range(max(1, args.warmup // 2)):
                        gsql(sql, timeout=1800)

                    times = []
                    for _ in range(args.rounds):
                        out, err = gsql(sql, timeout=1800)
                        t = parse_ms(out)
                        if t is not None:
                            times.append(t)

                    if len(times) >= args.rounds:
                        median = sorted(times)[len(times) // 2]
                        avg = sum(times) / len(times)
                        results[label] = {'median': median, 'avg': avg}
                        raw = ','.join('%d' % int(t) for t in times)
                        print("  %-20s median=%8.0fms  avg=%8.0fms  [%s]" % (label, median, avg, raw))
                    else:
                        results[label] = {'median': None}
                        print("  %-20s FAILED" % label)

        ALL_RESULTS[label] = results

    with open(args.output, 'w') as f:
        json.dump({'config': {'K': K_V, 'DOP': DOP_V, 'rounds': args.rounds,
                              'tables': [t[0] for t in targets], 'query_idx': args.query_idx},
                   'results': ALL_RESULTS,
                   'timestamp': str(datetime.now())}, f, indent=2, default=str)

    print("\n" + "=" * 60)
    print("  ALL DONE - results saved to %s" % args.output)
    print("=" * 60)
