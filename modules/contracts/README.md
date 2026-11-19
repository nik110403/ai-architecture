# Core module contracts

This directory describes portable event, result, and status records and validates
their declared compatibility boundary. It is metadata and generic validation, not
an installed adapter or a native-runtime claim. `mapping.json` keeps every planned
coverage ID concrete while its registry link is checked offline.

A native support claim must name an execution receipt (a JSON file in this
directory, linked by sha256). A receipt is tamper-evident and linked, not
authenticated, so an accepted claim still needs review of the run it names.

Permission model: reads only its own directory and the capability registry; no
network, no writes.
