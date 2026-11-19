"""Synthetic analysis: least-squares slope of y on x. Deterministic, stdlib only."""
import csv
from pathlib import Path

rows = list(csv.DictReader(open("data/input.csv", newline="")))
xs = [float(r["x"]) for r in rows]
ys = [float(r["y"]) for r in rows]
n = len(xs)
mx, my = sum(xs) / n, sum(ys) / n
slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
Path("06_tables").mkdir(exist_ok=True)
Path("05_figures").mkdir(exist_ok=True)
Path("06_tables/table1.csv").write_text(f"n,slope\n{n},{slope:.4f}\n")
Path("05_figures/fig1.txt").write_text("\n".join("*" * round(y) for y in ys) + "\n")
