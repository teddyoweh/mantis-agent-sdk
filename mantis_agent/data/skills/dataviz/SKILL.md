---
name: dataviz
description: Design charts that read at a glance and look professional (chart form, colour, labels, layout) for matplotlib, Excel charts, or any report or dashboard.
search_hint: chart charts graph graphs plot plots plotting visualization visualisation dataviz matplotlib seaborn plotly histogram palette dashboard
---
# Charts

A chart should make one point, and the title should say what it is. Everything
else on it either supports that point or gets removed.

## 1. Pick the form from the question

| Question | Form |
|---|---|
| Which is biggest? | Horizontal bars, sorted, with labels at the bar ends |
| How did it change over time? | A line for many points; columns for ≤ 12 periods. Mark the average or target with a thin dashed line |
| How does A compare with B in two periods? | Grouped bars, or a dot or slope chart |
| What makes up the whole? | One stacked bar, or sorted bars with % labels. A pie only for 2–3 parts |
| What drove the change? | A waterfall (bridge) from the start value to the end value |
| Many series over time? | Small multiples on shared axes, not one tangle of lines |
| Two different units? | Two charts. Never a dual axis |

Never use 3D, gradients, shadows, or exploded pies.

## 2. Colour

- One accent for the thing that matters and grey for context. Highlight the
  bar the text is about.
- Categorical colours: at most 6, and each category keeps its colour on every
  chart in the document. A colourblind-safe set (Okabe–Ito):
  `#0072B2 #E69F00 #009E73 #D55E00 #CC79A7 #56B4E9`.
- Neutrals: text `#1F2933`, secondary text `#5A6B7C`, gridlines `#E3E8EE`.
- Gains and losses: blue against vermillion (not red against green alone),
  with `+` and `−` signs on the labels.

## 3. Words on the chart

- Title = the finding ("Toys grew 122% from Q2 to Q3"). Subtitle = measure,
  units, period ("Revenue, $ thousands, Jan–Sep 2026").
- Label points and bars directly, and drop the legend when the labels fit.
- Format tick labels for people (`$400K`, `25%`, `Jan`), never `4e5`.
- Put a source line under the chart.

## 4. Anatomy

- Bars start at zero. Line charts may zoom in, but say so on the axis.
- Remove the top and right spines. Keep light horizontal gridlines, or none
  when every value is labelled.
- Use the same font, size and margins for every chart in a document, and
  export at 200 dpi with a tight bounding box.

## 5. matplotlib starter

```python
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.edgecolor": "#5A6B7C", "axes.labelcolor": "#1F2933", "text.color": "#1F2933",
    "axes.grid": True, "axes.grid.axis": "y", "grid.color": "#E3E8EE", "grid.linewidth": 0.7,
    "axes.axisbelow": True, "xtick.color": "#5A6B7C", "ytick.color": "#5A6B7C",
    "figure.dpi": 200, "savefig.dpi": 200, "savefig.bbox": "tight",
})
money_k = FuncFormatter(lambda v, _: f"${v / 1e3:,.0f}K")

fig, ax = plt.subplots(figsize=(6.5, 3.2))
bars = ax.barh(names, values, color=["#0072B2" if n == focus else "#B8C4D0" for n in names])
ax.bar_label(bars, labels=[f"${v / 1e3:,.0f}K" for v in values], padding=3, fontsize=8)
ax.invert_yaxis(); ax.xaxis.set_major_formatter(money_k); ax.grid(False)
ax.set_title("North leads, Central trails", loc="left", fontweight="bold")
fig.savefig("chart_region.png")
```

## 6. Check

Look at each chart at the size it will appear:
- labels are not clipped or overlapping;
- the legend doesn't cover data;
- colours match across charts;
- the numbers on the chart match the table next to it.
