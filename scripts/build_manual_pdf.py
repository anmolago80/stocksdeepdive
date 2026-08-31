"""
build_manual_pdf.py - regenerates StocksDeepDive_Manual.pdf from docs/manual.md.

Not part of the running app - a dev-time script, run manually whenever
manual.md changes (there was no committed build script for the existing
PDF; this replaces doing it by hand). Uses reportlab/platypus, matching
the generator of the more recent of the two pre-existing PDFs in the repo
root (StocksDeepDive_Manual_2026-08-30.pdf's own PDF metadata names
ReportLab as its Producer).

A small, deliberately plain Markdown-to-PDF converter - handles exactly
what manual.md actually uses (H1/H2/H3 headings, pipe tables, **bold**
spans, bullet lists, horizontal rules, plain paragraphs) and nothing
more. Not a general Markdown engine.

Run:  python3 scripts/build_manual_pdf.py
"""
import os
import re

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (HRFlowable, ListFlowable, ListItem,
                                 Paragraph, SimpleDocTemplate, Spacer,
                                 Table, TableStyle)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "docs", "manual.md")
OUT_CURRENT = os.path.join(ROOT, "StocksDeepDive_Manual.pdf")

NAVY = colors.HexColor("#0b1220")
TEAL = colors.HexColor("#0d9488")
GREY = colors.HexColor("#475569")
LINE = colors.HexColor("#cbd5e1")

styles = getSampleStyleSheet()
styles.add(ParagraphStyle("SDDTitle", parent=styles["Title"], textColor=NAVY,
                          fontSize=20, spaceAfter=4))
styles.add(ParagraphStyle("SDDMeta", parent=styles["Normal"], textColor=GREY,
                          fontSize=9.5, spaceAfter=14))
styles.add(ParagraphStyle("SDDH1", parent=styles["Heading1"], textColor=NAVY,
                          fontSize=15, spaceBefore=18, spaceAfter=8))
styles.add(ParagraphStyle("SDDH2", parent=styles["Heading2"], textColor=TEAL,
                          fontSize=12.5, spaceBefore=12, spaceAfter=6))
styles.add(ParagraphStyle("SDDH3", parent=styles["Heading3"], textColor=NAVY,
                          fontSize=11, spaceBefore=8, spaceAfter=4))
styles.add(ParagraphStyle("SDDBody", parent=styles["Normal"], fontSize=9.7,
                          leading=14, spaceAfter=8))
styles.add(ParagraphStyle("SDDCell", parent=styles["Normal"], fontSize=8.7,
                          leading=11.5))
styles.add(ParagraphStyle("SDDCellHead", parent=styles["SDDCell"],
                          textColor=colors.white, fontName="Helvetica-Bold"))


def _inline(text):
    """**bold** and `code` -> reportlab mini-markup; escape raw & < >
    first so stray characters in the source (there are a few — & in
    tickers, "<" from math phrasing) never get mistaken for XML."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', text)
    return text


def _table_flowable(rows):
    data = [[Paragraph(_inline(c), styles["SDDCellHead"] if i == 0 else styles["SDDCell"])
             for c in row] for i, row in enumerate(rows)]
    t = Table(data, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def parse(md_text):
    lines = md_text.split("\n")
    story = []
    i = 0
    bullet_buf = []

    def flush_bullets():
        if bullet_buf:
            story.append(ListFlowable(
                [ListItem(Paragraph(_inline(b), styles["SDDBody"])) for b in bullet_buf],
                bulletType="bullet", leftIndent=16, spaceAfter=8,
            ))
            bullet_buf.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped == "---":
            flush_bullets()
            story.append(HRFlowable(width="100%", color=LINE, thickness=0.7,
                                    spaceBefore=6, spaceAfter=10))
            i += 1
            continue

        if stripped.startswith("# "):
            flush_bullets()
            story.append(Paragraph(_inline(stripped[2:]), styles["SDDTitle"]))
            i += 1
            continue

        if stripped.startswith("## "):
            flush_bullets()
            story.append(Paragraph(_inline(stripped[3:]), styles["SDDH1"]))
            i += 1
            continue

        if stripped.startswith("### "):
            flush_bullets()
            story.append(Paragraph(_inline(stripped[4:]), styles["SDDH2"]))
            i += 1
            continue

        if stripped.startswith("#### "):
            flush_bullets()
            story.append(Paragraph(_inline(stripped[5:]), styles["SDDH3"]))
            i += 1
            continue

        if stripped.startswith("|"):
            flush_bullets()
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            rows = []
            for j, tl in enumerate(table_lines):
                if j == 1 and re.match(r"^\|[\s:|-]+\|$", tl):
                    continue  # separator row (---|---|---)
                cells = [c.strip() for c in tl.strip("|").split("|")]
                rows.append(cells)
            if rows:
                story.append(_table_flowable(rows))
                story.append(Spacer(1, 10))
            continue

        if stripped.startswith("- ") or stripped.startswith("* "):
            # A bullet's text often wraps onto following plain lines in the
            # source (no leading "- ") before the next bullet/blank/heading -
            # fold those into the same list item instead of letting them
            # fall through as an orphaned paragraph after the list.
            item_lines = [stripped[2:]]
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith(
                ("#", "|", "- ", "* ", "---")
            ):
                item_lines.append(lines[i].strip())
                i += 1
            bullet_buf.append(" ".join(item_lines))
            continue

        if stripped == "":
            flush_bullets()
            i += 1
            continue

        # Plain paragraph - collect contiguous non-blank, non-special lines.
        flush_bullets()
        para_lines = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith(
            ("#", "|", "- ", "* ", "---")
        ):
            para_lines.append(lines[i].strip())
            i += 1
        story.append(Paragraph(_inline(" ".join(para_lines)), styles["SDDBody"]))

    flush_bullets()
    return story


def main():
    with open(SRC, "r", encoding="utf-8") as f:
        md_text = f.read()

    story = parse(md_text)

    doc = SimpleDocTemplate(
        OUT_CURRENT, pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        title="StocksDeepDive - Complete User Manual",
        author="StocksDeepDive",
    )
    doc.build(story)
    print(f"wrote {OUT_CURRENT}")


if __name__ == "__main__":
    main()
