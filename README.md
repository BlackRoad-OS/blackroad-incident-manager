# BlackRoad Incident Manager

[![CI](https://github.com/BlackRoad-OS/blackroad-incident-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/BlackRoad-OS/blackroad-incident-manager/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: Proprietary](https://img.shields.io/badge/license-Proprietary-red.svg)](https://blackroad.io)

Production-grade incident management system for engineering teams. Track P1–P4 incidents, enforce SLA policies, compute MTTR analytics, and auto-generate postmortem documents — all from a single Python module backed by SQLite.

---

## Features

| Feature | Description |
|---------|-------------|
| **P1–P4 Severity** | Colour-coded severity tiers with distinct SLA targets |
| **Full Lifecycle** | `open → investigating → mitigating → resolved → closed` |
| **SLA Enforcement** | Automatic breach detection for response & resolution windows |
| **MTTR Analytics** | Per-severity & per-service breakdowns over rolling time windows |
| **Timeline Tracking** | Immutable, ordered event log per incident |
| **Postmortem Generator** | One-command markdown postmortem with all sections pre-filled |
| **Rich Terminal UI** | Colour-coded panels & tables (degrades gracefully without `rich`) |
| **SQLite Persistence** | Zero-config storage at `~/.blackroad/incident-manager.db` |
| **JSON & Markdown Export** | Full report export in both formats |
| **CLI + Python API** | Use from the shell or import directly |

---

## Severity Matrix

| Severity | Label | Description | Example |
|----------|-------|-------------|---------|
| 🔴 **P1** | Critical | Full customer-facing outage | Payments API down |
| 🟠 **P2** | High | Significant degradation | Auth latency > 5s |
| 🟡 **P3** | Medium | Limited / partial impact | Slow dashboard load |
| 🟢 **P4** | Low | Minor issue, workaround available | Typo in error message |

---

## SLA Targets

| Severity | Response SLA | Resolution SLA |
|----------|-------------|----------------|
| P1 | 15 minutes | 1 hour |
| P2 | 30 minutes | 4 hours |
| P3 | 2 hours | 24 hours |
| P4 | 8 hours | 72 hours |

SLA breach detection runs in real time against these defaults and is surfaced in dashboards, reports, and the `sla` command.

---

## Installation

```bash
# Clone
git clone https://github.com/BlackRoad-OS/blackroad-incident-manager.git
cd blackroad-incident-manager

# Optional: install rich for coloured terminal output
pip install rich

# Run tests
pip install pytest
pytest tests/ -v
```

No build step required. `incident_manager.py` uses only the Python standard library (+ optional `rich`).

---

## CLI Usage

### Create an incident

```bash
python incident_manager.py create "Payments API returning 503" P1 \
  --services payments checkout billing \
  --description "Elevated 503 rate from payments gateway" \
  --assignee alice
```

### Update status

```bash
python incident_manager.py update <ID> investigating --note "Checking gateway logs" --actor alice
python incident_manager.py update <ID> mitigating   --note "Traffic shifted to DR cluster"
```

### Resolve

```bash
python incident_manager.py resolve <ID> "Root cause: misconfigured load balancer rule. Rolled back." --actor alice
```

### List & filter

```bash
python incident_manager.py list                        # All open incidents
python incident_manager.py list --severity P1          # P1 only
python incident_manager.py list --status investigating  # In-progress
python incident_manager.py list --service payments      # By service
```

### Timeline

```bash
python incident_manager.py timeline <ID>
```

### Add a note

```bash
python incident_manager.py note <ID> "Engaged vendor support, ticket #45210" --actor bob
```

### Escalate

```bash
python incident_manager.py escalate <ID> P1 "Data corruption detected" --actor cto
```

### SLA status

```bash
python incident_manager.py sla <ID>
```

### Dashboard

```bash
python incident_manager.py dashboard
```

### MTTR statistics (last 30 days)

```bash
python incident_manager.py mttr
python incident_manager.py mttr --service payments --days 7
```

### Export report

```bash
python incident_manager.py report <ID>                   # Markdown
python incident_manager.py report <ID> --format json     # JSON
```

### Generate postmortem

```bash
python incident_manager.py postmortem <ID>
```

> **Tip:** You can use the first 8 characters of an incident ID as a short prefix — the CLI resolves it automatically.

---

## Python API

```python
from incident_manager import IncidentManager, EventType

mgr = IncidentManager()  # defaults to ~/.blackroad/incident-manager.db

# Create
inc = mgr.create_incident(
    title="Database connection pool exhausted",
    severity="P2",
    affected_services=["payments", "checkout"],
    description="Pool at 100%, queries timing out",
    assignee="alice",
)
print(inc.id, inc.status)  # <uuid>  open

# Progress through lifecycle
mgr.update_status(inc.id, "investigating", note="Checking slow query log", actor="alice")
mgr.add_timeline_entry(inc.id, EventType.PAGE_SENT.value, "Paged DBA on-call", "pagerduty",
                       metadata={"oncall": "bob", "channel": "sms"})
mgr.update_status(inc.id, "mitigating", note="Killed long-running queries", actor="alice")

# Resolve — automatically records resolved_at and computes MTTR
resolved = mgr.resolve(inc.id, "Connection pool drained; queries cleared", actor="alice")
print(f"MTTR: {resolved.mttr_minutes:.1f} minutes")

# SLA check
sla = mgr.check_sla_breach(inc.id)
print(sla["any_breach"])          # True / False
print(sla["response_breached"])   # True / False

# MTTR analytics (last 30 days)
stats = mgr.calculate_mttr(days=30)
print(stats["by_severity"]["P2"]["avg_mttr_mins"])

# Dashboard snapshot
dash = mgr.get_dashboard()
print(dash["open_by_severity"])   # {'P1': 0, 'P2': 1, 'P3': 2, 'P4': 0}
print(dash["sla_breach_count"])

# Export
report_md  = mgr.export_report(inc.id, format="markdown")
report_json = mgr.export_report(inc.id, format="json")

# Postmortem template
postmortem = mgr.postmortem_template(inc.id)
print(postmortem)
```

---

## Postmortem Example Output

```markdown
# Post-Mortem: Database connection pool exhausted

**Incident ID:** `3fa85f64-...`
**Severity:** P2 | **Status:** resolved
**Date:** 2025-06-14 | **MTTR:** 47.3 minutes
**SLA Status:** ✅ Within SLA

---

## Summary

> *(2–3 sentence overview: what happened, customer impact, how it was resolved.)*

**Affected Services:** payments, checkout
**Impact Summary:** Connection pool drained; queries cleared

---

## Timeline

- **2025-06-14T10:02:11Z** `[status_change]` **system:** Incident created with severity P2
- **2025-06-14T10:04:30Z** `[status_change]` **alice:** Status changed from 'open' to 'investigating' — Checking slow query log
- **2025-06-14T10:05:12Z** `[page_sent]` **pagerduty:** Paged DBA on-call
- **2025-06-14T10:18:44Z** `[status_change]` **alice:** Status changed from 'investigating' to 'mitigating' — Killed long-running queries
- **2025-06-14T10:49:29Z** `[status_change]` **alice:** Resolved in 47.3 min. Summary: Connection pool drained; queries cleared

---

## Root Cause

**Primary Root Cause:** *(e.g., Memory leak in auth-service caused by unbounded cache growth)*

---
...
```

---

## Architecture

```
incident_manager.py
├── Enums            IncidentSeverity, IncidentStatus, EventType
├── Dataclasses      Incident, IncidentEvent, SLAPolicy
├── IncidentManager  SQLite-backed core engine
│   ├── CRUD         create, get, list, update_status, resolve, assign, escalate
│   ├── Analytics    calculate_mttr, check_sla_breach, get_dashboard
│   └── Reporting    export_report, postmortem_template
├── Display          print_incident, print_incident_table, print_timeline, print_dashboard
└── CLI              argparse dispatcher with 12 subcommands
```

**Database:** `~/.blackroad/incident-manager.db`
Two tables: `incidents` (12 columns) + `incident_events` (7 columns), with indexes on status, severity, and incident_id.

---

## Running Tests

```bash
pip install pytest pytest-cov
pytest tests/ -v --cov=incident_manager --cov-report=term-missing
```

Expected: **50+ assertions** across 40 tests covering creation, lifecycle, timeline, MTTR, SLA breach, escalation, postmortem, export, dashboard, and filtering.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). All contributions are work-for-hire under BlackRoad OS, Inc.

---

## License

© 2025 BlackRoad OS, Inc. All rights reserved. Proprietary — not licensed for external use.
