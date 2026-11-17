"""Regenerate docs/MATRIX.md: worst report severity of every fixture x target translation.

Run with: uv run --extra dev --extra validate-azure --extra validate-agentspec python scripts/gen_matrix.py
"""
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from tests.test_matrix import ADAPTERS, SOURCES, TARGETS

cols = [f"{t}" + (f" ({'/'.join(o.values())})" if o else "") for t, o in TARGETS]
rows = {}
for name, f in SOURCES:
    ir, _ = ADAPTERS[name].parse(f)
    cells = []
    for target, opts in TARGETS:
        with tempfile.TemporaryDirectory() as d:
            cells.append(ADAPTERS[target].emit(ir, Path(d), opts).worst.name)
    rows.setdefault(name, []).append(cells)
lines = ["# Translation matrix", "",
         ("Number of fixtures of each source platform (rows) whose translation to each target (columns) "
         "ends with that worst report severity: `info` < `warning` < `lossy` < `manual`. `error` "
         "never occurs (the test suite fails on it). Regenerate with `scripts/gen_matrix.py`."), "",
         "| source (fixtures) \\ target | " + " | ".join(cols) + " |",
         "|---|" + "---|" * len(cols)]
for name, cells in rows.items():
    worst = []
    for i in range(len(cols)):
        c = Counter(r[i] for r in cells)
        worst.append(" · ".join(f"{c[o]} {o}" for o in ("manual", "lossy", "warning", "info")
                                if c[o]))
    lines.append(f"| {name} ({len(cells)}) | " + " | ".join(worst) + " |")
Path(__file__).parent.parent.joinpath("docs", "MATRIX.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
