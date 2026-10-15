# Codex adapter

The preparation review retrieved the official [Codex skills documentation](https://developers.openai.com/codex/skills/) on 2026-10-14; it redirected to the build-skills documentation. Its “Where Codex loads local skills” section says Codex scans `.agents/skills` from the current directory through the repository root. This is a supplied documentation observation, not a native-loading test or a claim that the page was re-fetched for this release.

Copy all of one selected `skills/<name>` directory into project-local `.agents/skills/<name>`, including its assets. Refuse an existing destination; inspect and merge manually. See [INSTALL](../INSTALL.md) for the collision-safe copy example and exact offline checks. Do not overwrite settings, rules or project instructions. Adapt role contracts and command bodies manually to the installed harness's native configuration. Claude agent/command metadata does not auto-import into Codex; no universal native agent import is claimed.
