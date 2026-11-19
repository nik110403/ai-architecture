# documents example

An offline walkthrough of the `documents` and `productivity` modules with synthetic data only.

    python3 examples/documents/run.py --offline --out OUT
    python3 examples/documents/verify_receipts.py

`run.py` creates and inspects a docx, an xlsx, a pptx and a pdf from the JSON files in `specs/`;
formula-checks the xlsx (and shows a wrong cached value naming its cell); packages the files with a
manifest; renders the pdf if `pdftotext` is observed (otherwise the step is recorded as `unavailable` with
the reason, never as a pass); reads, writes and conflicts on a copy of the sim store in `sim-store/`; shows
a missing connector refusing with the store unchanged; reads through the test connector `stub_connector.py`
declared in `connectors.json`; runs one authorized and one refused calendar add; writes a morning brief;
and previews, applies and rolls back a memory import. It prints one line per step, writes
a receipt file into `OUT`, and ends with `documents: OK`.

`verify_receipts.py` re-runs the example in a temp dir, or checks an existing receipt with
`--receipt FILE`. It exits 0 only if every expected step is present and passed (or `render pdf` is
`unavailable` with a reason) and every recorded artifact hash matches; it counts `unavailable` apart from
passes. A tampered receipt or artifact exits 1.

The authority file the example seals for the calendar add proves binding, expiry and single use, not that a
human issued it: the example mints it itself, which is exactly why receipts say
`authority_origin: "unverified"`. Nothing here sends, uploads or shares anything, contacts a provider, or
stores a credential. Single-use claims go under a `state` folder inside `OUT`.
