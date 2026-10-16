# fixture registry (live checks against a fake systemctl)
| Loop | Pattern | Tier | Hosts | Check | STATUS |
|---|---|---|---|---|---|
| `ok-timer` | Test | **L1** | any | timer:demo-ok.timer | ✅ live |
| `ok-service` | Test | **L1** | any | service:demo-ok.service | ✅ live |
| `idle-timer` | Test | **L1** | any | timer:demo-idle.timer | ⚠️ verify |
| `host-scoped` | Test | **L1** | other-host | timer:demo-missing.timer | ✅ live |
| `hook-row` | Test | **L1** | any | hook:demo-hook | ✅ live |
| `file-row` | Test | **L1** | any | file:loops-ok.md | ✅ live |
