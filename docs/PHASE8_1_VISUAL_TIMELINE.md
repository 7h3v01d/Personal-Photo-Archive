# Phase 8.1 — Visual Timeline Navigation

Phase 8.1 is presentation/navigation only. It does not alter the Phase 8.0
placement hierarchy or perform date inference.

## User experience

The Timeline window now provides:

- All → year → month navigation;
- separate Placed, Ranges, Tentative and Unplaced lanes;
- responsive thumbnail grids using the existing disk thumbnail cache;
- a provenance detail panel explaining the selected photo's placement;
- direct double-click/open into the existing Preview dialog;
- lane counts that update with the selected year/month.

## Safety / precision

- Confirmed ranges remain ranges; month indexing is navigation only.
- Tentative reconstruction proposals remain visually segregated.
- Unplaced material is never forced onto a chronological axis.
- No source-file, metadata, anchor, reconstruction or decision writes occur.
- Thumbnail decoding runs on a dedicated worker thread.
