# Provenance

Same `Field: value` format as the other provenance files here. The upstreams are an operator's historical installers, named only
by their logical role. `Source-sha256` is the sha256 of the source file as read on 2026-11-09. Nothing was copied: the primitive is new,
and the patterns file describes how the historical kinds map onto it.

## operator guard installer (historical)

Kit file: modules/installer/PATTERNS.md
Source: operator guard installer (historical)
Source-sha256: 3aa69b04f55aeb9efbba5b9b981496960d7c6ec528a00d14fbae854d252baf4b
State: re-authored

Deltas:
- only the shape (files plus a settings change) is kept as a pattern; the settings edit is left as a documented human step

## operator sandbox installer (historical)

Kit file: modules/installer/PATTERNS.md
Source: operator sandbox installer (historical)
Source-sha256: cd9a0ce286806e94297ff14a8720ba838e4154dee22a2104ba3661ab0771deb8
State: re-authored

Deltas:
- only the probe-then-print-the-install-line pattern is kept; nothing is installed with privileges

## Not ported

- the roughly thirty incident-specific install scripts (counted, not read for porting)
- any privileged step, network fetch or service restart
