---
name: pdf
description: Build client-ready PDF documents (reports, research notes, one-pagers) with reportlab and matplotlib, check them page by page before handing over, and read text out of existing PDFs.
search_hint: pdf pdfs reportlab pdftotext pdftoppm pypdf brochure one-pager memo
---
# PDF documents

The bar is a document you could send to a client without a second look: the
answer first, every number right, every page clean. Most of the quality comes
from the check loop at the end.

**Save a complete version early.** As soon as the numbers are in, build a
plain but correct `report.pdf` with every required part. Then improve it. If
you run out of turns, a finished simple document beats a clever half-built one.
And if a layout trick still fails after two tries, drop it.

**Match the effort to the ask.** A quick report, with one table and a chart,
needs one build, one look at each page, and one numbers check. Then stop.
The full treatment (sections 2 and 4 in depth, two or three polish rounds) is
for documents meant to be sent: client-ready, board or MD quality, a research
note.

## 1. Analysis before layout

- Compute everything in a script first and print it: totals, shares, changes,
  the drivers behind the biggest change. Keep the figures in one dict that the
  report reads from. Never retype a number into prose by hand.
- Ask what explains the headline, not only what it is. Example: a category
  grew 120% because of volume or price, and from which regions. One driver
  breakdown beats three more charts.
- Write the takeaways before the layout: 3–5 sentences, each with a number.

## 2. Structure

1. Title block: title, one-line subtitle (scope and period), prepared date,
   then a strip of 3–5 headline figures.
2. Executive summary: the takeaways, numbered or in bold lead-ins.
3. Sections. Each one answers a question its heading asks ("Which category grew
   most, and why?") and opens with the answer in one sentence.
4. Exhibits, numbered across the document. Each has a title that states the
   finding, a units line, the chart or table, and a `Source:` line.
5. A methodology / data note: source and row count, definitions (for example
   revenue = units × price), period conventions, rounding, caveats.
6. An appendix for the detailed tables, if they would interrupt the story.

## 3. Building it with reportlab

- Use platypus (`SimpleDocTemplate`) with an `onPage` callback for the
  running header (short title, period). For `Page X of Y` use this canvas
  maker, a known-good recipe. Don't write your own two-pass build:

  ```python
  from reportlab.pdfgen import canvas

  class NumberedCanvas(canvas.Canvas):
      def __init__(self, *a, **k):
          super().__init__(*a, **k)
          self._pages = []
      def showPage(self):
          self._pages.append(dict(self.__dict__))
          self._startPage()
      def save(self):
          for state in self._pages:
              self.__dict__.update(state)
              self.setFont("Helvetica", 7.5)       # or your registered font
              self.drawRightString(self._pagesize[0] - 54, 30,
                                   f"Page {self._pageNumber} of {len(self._pages)}")
              super().showPage()
          super().save()

  doc.build(story, onFirstPage=header, onLaterPages=header, canvasmaker=NumberedCanvas)
  ```
- A contents list is optional. A short report reads fine without one. If you
  add one, build once, read each section's page from the output
  (`pdftotext`), then fill the numbers in and build again. Never guess them.
- Typography: one family throughout. If the PDF leaves your machine, embed a
  TTF. DejaVu Sans ships with matplotlib, at
  `matplotlib.get_data_path() + "/fonts/ttf/DejaVuSans.ttf"`. Suggested sizes:
  title 22–26 pt, headings 13–16, body 9–10.5, captions and sources 7–8.
  Leading is about 1.3× the font size.
- Tables:
  - Numbers right-aligned, with thousands separators and the same decimals
    down a column.
  - Total row bold with a rule above, light zebra rows or hairlines.
  - `repeatRows=1`, explicit `colWidths`, and wrap long cells in `Paragraph`.
- Charts: matplotlib at 200 dpi, following the `dataviz` skill. Size the
  image in points to the frame width and keep the aspect ratio.
- Keep things together: `KeepTogether([title, chart, source])`, and
  `keepWithNext` on headings or a `CondPageBreak` before a section, so no
  heading sits alone at the bottom of a page.
- Colour: one dark ink (navy or charcoal), one accent, greys. The same colour
  means the same thing on every page.

## 4. Check before you finish (do not skip)

1. **Look at every page.** Run `pdftoppm -png -r 80 report.pdf _check/p`, then
   `read_file` each PNG. Look for:
   - clipped or overlapping labels;
   - charts running into the next heading;
   - half-empty pages and stray single lines;
   - inconsistent styles and leftover placeholder text.

   Fix, re-render and look again. For a client-ready document, two or three
   rounds is normal. For a quick report, stop once a look turns up nothing
   wrong.

   If an image comes back as omitted (the model can't view images), check
   structure instead:
   - `pdftotext -layout report.pdf -` page by page;
   - in the build script, compare each flowable's height with the frame's;
   - make sure no table or image is wider than the frame.
2. **Recompute the numbers independently.** Use a short second script that
   works differently, for example `csv` + `Decimal` instead of pandas.
   Recompute every figure the document cites and compare it with the
   `pdftotext` output:
   - parts sum to totals;
   - shares sum to 100% (allowing for rounding, which the note should mention);
   - the date range matches the data.
3. **Fonts:** in `pdffonts report.pdf`, every font should say `emb yes`.
4. **Finish:** page numbers on every page, the title and dates right, the file
   opens (`python3 -c "import pypdf; pypdf.PdfReader('report.pdf')"`).

## Reading an existing PDF

- `read_file` on a `.pdf` returns its text page by page. Use `pages="1-5"` for
  a range.
- `pdftotext -layout file.pdf -` keeps columns. `pdfplumber`, if installed,
  pulls tables. `pdftoppm -png -r 80 -f 3 -l 3 file.pdf out` renders page 3 to
  look at.
