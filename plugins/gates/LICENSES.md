# Licences for ai-architecture-gates

- Code, tests and configuration in this plugin: MIT, same as the main collection. A copy of the repository notice is included in this plugin's `LICENSE`.
- `README.md` and this file are documentation, covered like the collection's other docs.

## Confirmation tier of the destructive-command guard

The "ask" rule list in `hooks/bash-destructive-guard.py` (force-push, `git reset --hard`, whole-tree
`git checkout .` / `git restore .`, `DROP TABLE`, `TRUNCATE`, `kubectl delete`, `docker rm -f`,
`docker system prune`) is adapted from the safety-hook pattern list in the **gstack** project
(`garrytan/gstack`), whose licence was checked against the GitHub API on 2026-10-16 and reported as
MIT (SPDX id). The command shapes are patterns adapted from that project; the surrounding code is
written for this plugin. Attribution: gstack, `Copyright (c) 2026 Garry Tan`, MIT. If you redistribute this plugin, keep this attribution and the licence text below.

gstack licence (verbatim from `garrytan/gstack` main, fetched 2026-10-16):

```text
MIT License

Copyright (c) 2026 Garry Tan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Other material

No other third-party code is included. Each hook's origin and changes are listed in
`hooks/PROVENANCE.md`.
