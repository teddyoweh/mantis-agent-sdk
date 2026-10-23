---
name: xlsx
description: Build or edit Excel workbooks with openpyxl (analyst models, summaries, trackers) with live formulas, proper tables, formats and charts, then recalculate and check them before handing over.
search_hint: excel xlsx xlsm spreadsheet spreadsheets workbook openpyxl sumif sumifs vlookup pivot
---
# Excel workbooks

Build a workbook someone can audit: every derived number is a formula they can
click on, inputs are obvious, and the totals tie out. openpyxl writes formulas
but never calculates them, so an unchecked file can hide `#NAME?` or a range
that stops short. The check step below is part of the job.

**Match the effort to the ask.** For a simple workbook (a data sheet, a
summary, a chart), one recalculation, one comparison with the raw data, and
the two basics below are enough; then stop. The full conventions, check cells and a look at the
rendered sheets are for models someone will rely on or send on.

## 1. Conventions of a model people trust

- **Live formulas for everything derived.** Hard-code only inputs, and say
  where each input comes from. Never paste a computed value.
- **No numbers buried in formulas.** Write `=B5*Assumptions!$C$3`, not
  `=B5*1.07`.
- **One pattern per row or column.** The same formula is copied across, never
  a one-off cell in the middle of a block.
- **Colour code:**
  - hard-coded inputs in blue font (`0000FF`);
  - formulas in black;
  - links to other sheets in green (`008000`);
  - optionally, a light yellow fill on cells meant to be changed.
- **Check cells.** Each tie-out (regions vs categories vs grand total, months
  vs total) gets a visible difference cell that should read 0, labelled
  "Check".
- **Order the sheets:** summary or cover first, then working sheets, then raw
  data, unless the task says otherwise.

## 2. openpyxl recipes

```python
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.chart import BarChart, LineChart, Reference

MONEY = '$#,##0;($#,##0);"–"'
PCT = '0.0%'
INPUT = Font(color="0000FF")
HEAD = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
```

- **Types first.** `csv.reader` gives strings. Numbers written as text look
  wrong in Excel (green corner marks, left-aligned) and break `SUM` and
  sorting. Read the CSV with pandas, or convert each field: `int(units)`,
  `float(price)`, `date.fromisoformat(d)`.
- **Raw data stays raw.** On the data sheet, headers go in row 1, data from
  row 2, and nothing above or beside it. Pivot tables, Power Query and other
  people's formulas expect that shape. Titles and notes belong on the
  presentation sheets.
- **Raw data as an Excel Table.** It gives filter buttons and banding:
  `ws.add_table(Table(displayName="Sales", ref=f"A1:F{n+1}",
  tableStyleInfo=TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)))`.
  Add `ws.freeze_panes = "A2"` and set the number formats per column.
- **Row formulas:** `ws[f"F{r}"] = f"=D{r}*E{r}"` on every row.
- **Aggregates:**
  `=SUMIFS(Data!$F$2:$F$1501,Data!$B$2:$B$1501,$A5)`.
  - Use absolute ranges sized to the data. Whole-column references are slow,
    and some tools can't evaluate structured `Table[col]` references.
  - By month: add a helper month column to Data, or bound the dates:
    `=SUMIFS(Data!$F:$F,Data!$A:$A,">="&DATE(2026,1,1),Data!$A:$A,"<"&DATE(2026,2,1))`.
  - Share of total: `=B5/B$11`, formatted `0.0%`.
  - Grand total: `=SUM(B5:B9)`, then a check cell
    `=B11-SUM(Data!$F$2:$F$1501)`.
- **Presentation sheets:**
  - A title in A1 and a subtitle in A2 (source, period, units).
  - Header rows bold on a dark fill, with a thin rule under them.
  - Set column widths explicitly (`ws.column_dimensions["A"].width = 18`).
  - `ws.sheet_view.showGridLines = False`; number formats on every numeric cell.
- **Charts:** reference the formula cells (`Reference(ws, min_col=2,
  min_row=4, max_row=9)`).
  - Set the title, the axis number format (`chart.y_axis.numFmt = '$#,##0'`)
    and the size (`chart.width = 16; chart.height = 8`).
  - Drop the legend for a single series.
  - Anchor charts beside their table, never on top of it.
  - Follow the `dataviz` skill for form and colour.
- **Notes block:** source file and row count, definitions, method, and the
  date it was prepared.

## 3. Recalculate and check before you finish

0. **The basics, always.** Every column has its header, including the ones you
   added, such as `revenue`. Every number is stored as a number: reopen the
   file and check `type(ws["D2"].value)`.
1. **Recalculate**, in this order of preference:
   - LibreOffice, if installed:
     `soffice --headless --convert-to xlsx --outdir _recalc summary.xlsx`,
     then `openpyxl.load_workbook("_recalc/summary.xlsx", data_only=True)`.
   - Otherwise a Python evaluator: `pip install pycel` (or `formulas`), then
     `ExcelCompiler(filename="summary.xlsx").evaluate("Summary!B5")` for
     every formula cell you care about.
   - Otherwise a small evaluator for the few functions you used.
2. **Compare with an independent computation** from the raw data (pandas or
   csv): every total, share and month × category cell. Print a pass/fail
   table, and fix anything that fails.
3. **Scan formulas** for `#REF!`, `#DIV/0!`, `#NAME?` and `#VALUE!`, ranges
   that stop before the last data row, and relative references that should be
   absolute.
4. **Look at it.**
   - On macOS, `qlmanage -t -s 2000 -o _qa summary.xlsx` renders the first
     sheet to a PNG.
   - LibreOffice can `--convert-to pdf` for all sheets; then `pdftoppm` them.
   - `read_file` the images and check: no `####` columns, formats applied,
     charts placed and readable.
   - If you can't view images, re-read the workbook with openpyxl instead:
     column widths, number formats and each chart's anchor.
5. **Reopen** the saved file with `openpyxl.load_workbook` to be sure it isn't
   corrupt.
