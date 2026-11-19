# Installer patterns

How the one-off installers an operator accumulates map onto one manifest-driven primitive. Described, not executed.

## Many one-off fix installers become one manifest

Each historical installer copied a few files, patched a setting or repaired a particular incident. A manifest lists the files,
their modes and hashes once; `apply` is idempotent, `rollback` undoes it, and a changed user file is preserved. A new fix is a
new manifest version, not a new script.

## An installer with a pinned third-party package

Pin the artifact by hash, verify the hash before anything is installed, and fail closed on a mismatch or a missing file. This
installer does not fetch anything: the bytes must already be present beside the manifest. Obtaining them is a separate, human step.

## A sandbox installer

Probe first (is the capability already there?), then print the documented install line for a person to run. A probe that fails
is reported as unavailable; nothing falls back to running unsandboxed.

## A guard installer

A guard hook set is a list of files plus a settings change. The files fit a manifest. The settings change touches a file the user
owns and edits, so it stays a documented step rather than an automatic one.

## Deliberately left out

- incident-specific repair scripts: they encode one machine's history and have no general form
- anything that needs a privileged step, a network fetch or a service restart
- writing outside the named target root
