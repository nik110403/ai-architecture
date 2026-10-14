# Release checklist

- [ ] Source review: every shipped file is intentional and covered by the fixed inventory.
- [ ] Privacy review: scan the source tree and extracted archive for private paths, hosts, tokens, and personal data.
- [ ] Usability review: a fresh reader can follow INSTALL and RUN without the parent repository.
- [ ] Run `python3 scripts/package_preview.py` and the commands in `RUN.md`.
- [ ] Confirm MIT for code and CC BY 4.0 for documentation.
- [ ] Follow up separately on the NOT RUN checks below; neither is a preview blocker.
- [ ] Obtain explicit approval for the final public name and public push before publication.

## Validation status

- OBSERVED: the fixture ran locally on Linux with Python 3.12.
- NOT RUN: macOS validation.
- NOT RUN: Windows validation.
- NOT RUN: native Claude Code instruction loading.
- NOT RUN: native Codex instruction loading.
- NOT RUN: native OpenCode instruction loading.
- NOT MEASURED: comparative benefit across platforms or native instruction loaders.
