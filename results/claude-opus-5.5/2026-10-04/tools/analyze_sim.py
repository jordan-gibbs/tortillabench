"""Local QA of a bake-only run: plots the exported cloth meshes (no rendering involved).

    python tools/analyze_sim.py bakes/bake01
"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import PolyCollection


def load_obj(path):
    v, f = [], []
    for line in open(path):
        if line.startswith("v "):
            v.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("f "):
            f.append([int(x) - 1 for x in line.split()[1:]])
    return np.array(v), np.array(f)


def main(d):
    d = Path(d)
    rest, faces = load_obj(d / "sim_rest.obj")
    names = sorted(p.stem for p in d.glob("sim_*.obj") if p.stem != "sim_rest")
    fig, axes = plt.subplots(len(names), 3, figsize=(18, 5 * len(names)))
    for row, name in zip(np.atleast_2d(axes), names):
        v, _ = load_obj(d / f"{name}.obj")
        z = v[:, 2] * 1000
        tri = v[faces]
        zc = tri[:, :, 2].mean(1) * 1000
        order = np.argsort(zc)
        pc = PolyCollection(tri[order][:, :, :2] * 100, array=zc[order], cmap="viridis", edgecolors="none")
        row[0].add_collection(pc)
        row[0].autoscale()
        row[0].set_aspect("equal")
        row[0].set_title(f"{name}: top view (cm), colour = z mm [{z.min():.1f}, {z.max():.1f}]")
        plt.colorbar(pc, ax=row[0])
        # side views, painter's order along the view axis
        for ax, (a, b, depth, lab) in zip(row[1:], ((0, 2, 1, "x-z (from -y)"), (1, 2, 0, "y-z (from +x)"))):
            dep = tri[:, :, depth].mean(1)
            o = np.argsort(-dep if depth == 1 else dep)
            pcs = PolyCollection(np.stack([tri[o][:, :, a] * 100, tri[o][:, :, b] * 1000], -1),
                                 array=rest[faces][o][:, :, 1].mean(1), cmap="coolwarm", edgecolors="k",
                                 linewidths=0.05)
            ax.add_collection(pcs)
            ax.autoscale()
            ax.set_title(f"{name}: {lab}  (horiz cm, vert mm)")
    fig.tight_layout()
    out = d / "sim_qa.png"
    fig.savefig(out, dpi=70)
    print("wrote", out)

    # layer separation in the final state: for every vertex, the nearest vertex that was >2 cm away at rest,
    # restricted to small horizontal offsets so it measures the stack gap
    v, _ = load_obj(d / "sim_final_cloth.obj")
    n = len(v)
    res = []
    for i in range(n):
        dxy = np.hypot(v[:, 0] - v[i, 0], v[:, 1] - v[i, 1])
        dr = np.linalg.norm(rest - rest[i], axis=1)
        m = (dxy < 0.0015) & (dr > 0.02)
        if m.any():
            res.append(np.abs(v[m, 2] - v[i, 2]).min())
    res = np.array(res) * 1000
    if len(res):
        print(f"stacked verts {len(res)}/{n}; vertical gap to nearest other layer (mm): "
              f"min {res.min():.2f}  p05 {np.percentile(res, 5):.2f}  p50 {np.median(res):.2f}  "
              f"p95 {np.percentile(res, 95):.2f}")
        print(f"verts with gap < 1.9 mm: {(res < 1.9).sum()}, < 1.5 mm: {(res < 1.5).sum()}")
    print(f"final z range {v[:, 2].min() * 1000:.2f} .. {v[:, 2].max() * 1000:.2f} mm; "
          f"footprint x {np.ptp(v[:, 0]) * 100:.1f} cm, y {np.ptp(v[:, 1]) * 100:.1f} cm")
    # edge stretch
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    l0 = np.linalg.norm(rest[e[:, 0]] - rest[e[:, 1]], axis=1)
    l1 = np.linalg.norm(v[e[:, 0]] - v[e[:, 1]], axis=1)
    s = l1 / l0
    print(f"edge strain: min {s.min():.3f} p01 {np.percentile(s, 1):.3f} p99 {np.percentile(s, 99):.3f} max {s.max():.3f}")


if __name__ == "__main__":
    main(sys.argv[1])
