# Real screenshot fixtures

The spec (§12) calls for 2-3 real listing screenshots as OCR test fixtures,
in addition to the synthetic PIL-generated images already covered by
`tests/test_ocr.py`. None are checked in yet — add your own screenshots of
listing pages you've personally visited here (PNG/JPG), since they're not
redistributable and have to come from an actual browsing session.

Once added, extend `tests/test_ocr.py` / `tests/test_extract.py` with tests
that point at these files and assert extraction recovers the known price,
tax, etc. for each one.
