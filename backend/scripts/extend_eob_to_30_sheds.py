"""
Extend the "Beaufort 86" (renamed to Current Batch on some rebuilds) sheet
in batch-results.xlsx from 6 shed-blocks (max 18 sheds) to 10 shed-blocks
(max 30 sheds) so growers with 30 sheds see all of them in End-of-Batch.

Each shed-block is 16 rows:
  row+0  "placement" label + 3 placement values (cols C, M, W)
  row+1  "morts"     label + 3 morts values      + 3 mortality % formulas
  row+2..+11  individual catch rows (birds, date, age, sex, ave wgt, total wgt)
  row+12 totals row (total caught, ave wgt, total wgt, FCR, cFCR)
  row+13 SHED N label row (Date/Age/Sex column headers)
  row+14, +15  spacers / blank

Blocks currently start at rows 4, 20, 36, 52, 68, 84 (6 blocks).
Target: also add blocks at rows 100, 116, 132, 148 → 10 blocks × 3 = 30 sheds.

Feb 28, 2026 (Jason): "end of batch when when set up eg 30 sheds only showing 12".
"""
import shutil
from copy import copy
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter

XLSX = Path("/app/silo/artifacts/feed-program/public/batch-results.xlsx")


def copy_row(ws, src_row: int, dst_row: int) -> None:
    """Copy cell value + style from src_row to dst_row across every column."""
    for c in range(1, ws.max_column + 1):
        s = ws.cell(row=src_row, column=c)
        d = ws.cell(row=dst_row, column=c)
        # Value: if the source is a formula, adjust row references
        v = s.value
        if isinstance(v, str) and v.startswith("=") and "$" not in v:
            # Cheap row-reference bump: replace 'A5' → 'A<dst_row + row_delta>'
            # For our template, formulas within a block reference cells inside
            # the same block (e.g. =B5/B4 in morts row uses same-block placement).
            # So we shift row refs by dst_row - src_row.
            delta = dst_row - src_row
            import re as _re
            def _bump(m):
                col, row = m.group(1), int(m.group(2))
                return f"{col}{row + delta}"
            v = _re.sub(r"([A-Z]+)(\d+)", _bump, v)
        d.value = v
        # Style (font, fill, border, alignment, number_format, protection)
        if s.has_style:
            d.font = copy(s.font)
            d.fill = copy(s.fill)
            d.border = copy(s.border)
            d.alignment = copy(s.alignment)
            d.number_format = s.number_format
            d.protection = copy(s.protection)
    # Row height
    try:
        rh = ws.row_dimensions[src_row].height
        if rh: ws.row_dimensions[dst_row].height = rh
    except Exception:
        pass


def main() -> None:
    backup = XLSX.with_suffix(".pre-30sheds-backup.xlsx")
    if not backup.exists():
        shutil.copy2(XLSX, backup)
        print(f"  ↳ backup saved: {backup.name}")

    wb = openpyxl.load_workbook(XLSX, data_only=False)
    ws = wb[wb.sheetnames[0]]  # first sheet — "Beaufort 86" or "Current Batch"

    src_start = 4
    src_end = 19  # 16-row block: rows 4..19 inclusive
    block_len = src_end - src_start + 1  # 16
    existing_block_starts = [4, 20, 36, 52, 68, 84]
    next_start = existing_block_starts[-1] + block_len  # 100
    new_block_starts = [next_start, next_start + block_len, next_start + 2 * block_len, next_start + 3 * block_len]

    print(f"  Duplicating rows {src_start}-{src_end} to new blocks: {new_block_starts}")

    for new_start in new_block_starts:
        for offset in range(block_len):
            src = src_start + offset
            dst = new_start + offset
            copy_row(ws, src, dst)
        # Update the SHED label on row +13 (row 4+15=19 is the "SHED 4" label).
        # In each new block, this label lands at new_start + 15. Increment shed
        # number so each block shows the right shed group.
        # Existing labels: block 4→"SHED 4", block 20→"SHED 5", etc — likely
        # driven by hand. Auto-bump by 1 per new block from the last existing.
        label_row = new_start + 15
        # Bump the numeric suffix if present
        prev_label = ws.cell(row=new_start - 16 + 15, column=1).value  # previous block's label
        if isinstance(prev_label, str):
            import re as _re
            m = _re.search(r"(\d+)$", prev_label.strip())
            if m:
                nxt = int(m.group(1)) + 1
                new_label = _re.sub(r"\d+$", str(nxt), prev_label.strip())
                ws.cell(row=label_row, column=1).value = new_label
                print(f"    row {label_row} col A: {prev_label.strip()!r} → {new_label!r}")

    wb.save(XLSX)
    print(f"  ✓ Saved. Sheet now supports 10 blocks × 3 sheds = 30 sheds.")


if __name__ == "__main__":
    main()
