# Integration, status, removal

Install: copy `modules/review/`. To make the gates plugin honour receipts, set `review_receipts_dir` (an absolute
directory you own) in your user-scope gates config and have your reviewer step run `receipt.py issue` after each
review; without that key nothing changes.

Status: `claimed_support` is `planned` in `descriptor.json`; this module has no recorded live evidence.

Removal: delete `modules/review/` and unset `review_receipts_dir`. Receipt files are ordinary JSON and stay.
