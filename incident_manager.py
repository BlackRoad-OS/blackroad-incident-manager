#!/usr/bin/env python3
"""
BlackRoad Incident Manager — Production-grade incident management system.

Features:
  - P1–P4 severity classification with SLA tracking
  - Full incident lifecycle (open → investigating → mitigating → resolved → closed)
  - MTTR analytics with per-severity and per-service breakdowns
  - Auto-generated postmortem templates (markdown)
  - Full incident export (markdown + JSON)
  - Rich terminal UI (degrades gracefully without `rich`)
  - SQLite persistence at ~/.blackroad/incident-manager.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    HAS_RICH = True
except ImportError:
    HAS_RICH = False

_console: Optional["Console"] = Console() if HAS_RICH else None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class IncidentSeverity(str, Enum):
    P1 = "P1"  # Critical  — full customer-facing outage
    P2 = "P2"  # High      — significant degradation
    P3 = "P3"  # Medium    — limited / partial impact
    P4 = "P4"  # Low       — minor issue, workaround available


class IncidentStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    MITIGATING = "mitigating"
    RESOLVED = "resolved"
    CLOSED = "closed"


class EventType(str, Enum):
    STATUS_CHANGE = "status_change"
    NOTE = "note"
    ESCALATION = "escalation"
    ACTION_TAKEN = "action_taken"
    EXTERNAL_LINK = "external_link"
    PAGE_SENT = "page_sent"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class IncidentEvent:
    id: str
    incident_id: str
    event_type: str
    actor: str
    message: str
    timestamp: str
    metadata: dict = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        incident_id: str,
        event_type: str,
        actor: str,
        message: str,
        metadata: Optional[dict] = None,
    ) -> "IncidentEvent":
        return cls(
            id=str(uuid.uuid4()),
            incident_id=incident_id,
            event_type=event_type,
            actor=actor,
            message=message,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata=metadata or {},
        )


@dataclass
class SLAPolicy:
    severity: str
    response_time_mins: int
    resolution_time_mins: int


@dataclass
class Incident:
    id: str
    title: str
    description: str
    severity: str
    status: str
    affected_services: list
    assignee: Optional[str]
    created_at: str
    resolved_at: Optional[str]
    tags: list
    timeline: list
    impact_summary: Optional[str]
    runbook_url: Optional[str]

    @property
    def mttr_minutes(self) -> Optional[float]:
        if not self.resolved_at:
            return None
        created = datetime.fromisoformat(self.created_at)
        resolved = datetime.fromisoformat(self.resolved_at)
        return round((resolved - created).total_seconds() / 60, 2)

    @classmethod
    def new(
        cls,
        title: str,
        severity: str,
        affected_services: list,
        description: str = "",
        assignee: Optional[str] = None,
    ) -> "Incident":
        return cls(
            id=str(uuid.uuid4()),
            title=title,
            description=description,
            severity=severity,
            status=IncidentStatus.OPEN.value,
            affected_services=affected_services,
            assignee=assignee,
            created_at=datetime.now(timezone.utc).isoformat(),
            resolved_at=None,
            tags=[],
            timeline=[],
            impact_summary=None,
            runbook_url=None,
        )


# ---------------------------------------------------------------------------
# SLA Policies
# ---------------------------------------------------------------------------

DEFAULT_SLA: dict[str, SLAPolicy] = {
    IncidentSeverity.P1.value: SLAPolicy("P1", response_time_mins=15, resolution_time_mins=60),
    IncidentSeverity.P2.value: SLAPolicy("P2", response_time_mins=30, resolution_time_mins=240),
    IncidentSeverity.P3.value: SLAPolicy("P3", response_time_mins=120, resolution_time_mins=1440),
    IncidentSeverity.P4.value: SLAPolicy("P4", response_time_mins=480, resolution_time_mins=4320),
}

SEVERITY_COLORS = {
    "P1": "bold red",
    "P2": "bold yellow",
    "P3": "bold cyan",
    "P4": "bold green",
}
STATUS_COLORS = {
    "open": "red",
    "investigating": "yellow",
    "mitigating": "blue",
    "resolved": "green",
    "closed": "dim",
}


# ---------------------------------------------------------------------------
# IncidentManager
# ---------------------------------------------------------------------------


class IncidentManager:
    """Core incident management engine backed by SQLite."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            db_dir = Path.home() / ".blackroad"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(db_dir / "incident-manager.db")
        self.db_path = db_path
        self.sla_policies: dict[str, SLAPolicy] = DEFAULT_SLA.copy()
        self._init_db()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    id                TEXT PRIMARY KEY,
                    title             TEXT NOT NULL,
                    description       TEXT,
                    severity          TEXT NOT NULL,
                    status            TEXT NOT NULL,
                    affected_services TEXT,
                    assignee          TEXT,
                    created_at        TEXT NOT NULL,
                    resolved_at       TEXT,
                    tags              TEXT,
                    impact_summary    TEXT,
                    runbook_url       TEXT
                );
                CREATE TABLE IF NOT EXISTS incident_events (
                    id          TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    event_type  TEXT NOT NULL,
                    actor       TEXT NOT NULL,
                    message     TEXT NOT NULL,
                    timestamp   TEXT NOT NULL,
                    metadata    TEXT,
                    FOREIGN KEY (incident_id) REFERENCES incidents(id)
                );
                CREATE INDEX IF NOT EXISTS idx_events_incident
                    ON incident_events(incident_id);
                CREATE INDEX IF NOT EXISTS idx_incidents_status
                    ON incidents(status);
                CREATE INDEX IF NOT EXISTS idx_incidents_severity
                    ON incidents(severity);
                """
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _incident_to_row(self, inc: Incident) -> tuple:
        return (
            inc.id,
            inc.title,
            inc.description,
            inc.severity,
            inc.status,
            json.dumps(inc.affected_services),
            inc.assignee,
            inc.created_at,
            inc.resolved_at,
            json.dumps(inc.tags),
            inc.impact_summary,
            inc.runbook_url,
        )

    def _row_to_incident(self, row: tuple, events: list) -> Incident:
        (
            id_,
            title,
            description,
            severity,
            status,
            affected_services,
            assignee,
            created_at,
            resolved_at,
            tags,
            impact_summary,
            runbook_url,
        ) = row
        return Incident(
            id=id_,
            title=title,
            description=description or "",
            severity=severity,
            status=status,
            affected_services=json.loads(affected_services or "[]"),
            assignee=assignee,
            created_at=created_at,
            resolved_at=resolved_at,
            tags=json.loads(tags or "[]"),
            timeline=events,
            impact_summary=impact_summary,
            runbook_url=runbook_url,
        )

    def _load_events(self, conn: sqlite3.Connection, incident_id: str) -> list:
        rows = conn.execute(
            "SELECT id, incident_id, event_type, actor, message, timestamp, metadata "
            "FROM incident_events WHERE incident_id = ? ORDER BY timestamp",
            (incident_id,),
        ).fetchall()
        return [
            IncidentEvent(
                id=r[0],
                incident_id=r[1],
                event_type=r[2],
                actor=r[3],
                message=r[4],
                timestamp=r[5],
                metadata=json.loads(r[6] or "{}"),
            )
            for r in rows
        ]

    def _save_incident(self, conn: sqlite3.Connection, inc: Incident) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            self._incident_to_row(inc),
        )

    def _save_event(self, conn: sqlite3.Connection, evt: IncidentEvent) -> None:
        conn.execute(
            "INSERT INTO incident_events VALUES (?,?,?,?,?,?,?)",
            (
                evt.id,
                evt.incident_id,
                evt.event_type,
                evt.actor,
                evt.message,
                evt.timestamp,
                json.dumps(evt.metadata),
            ),
        )

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def create_incident(
        self,
        title: str,
        severity: str,
        affected_services: list,
        description: str = "",
        assignee: Optional[str] = None,
    ) -> Incident:
        """Create a new incident and open it immediately."""
        if severity not in [s.value for s in IncidentSeverity]:
            raise ValueError(f"Invalid severity {severity!r}. Must be one of P1/P2/P3/P4.")
        inc = Incident.new(title, severity, affected_services, description, assignee)
        evt = IncidentEvent.new(
            inc.id,
            EventType.STATUS_CHANGE.value,
            "system",
            f"Incident created with severity {severity}",
            {"initial_severity": severity, "initial_status": IncidentStatus.OPEN.value},
        )
        inc.timeline.append(evt)
        with sqlite3.connect(self.db_path) as conn:
            self._save_incident(conn, inc)
            self._save_event(conn, evt)
        return inc

    def update_status(
        self,
        incident_id: str,
        new_status: str,
        note: str = "",
        actor: str = "system",
    ) -> Incident:
        """Transition an incident to a new lifecycle status."""
        if new_status not in [s.value for s in IncidentStatus]:
            raise ValueError(f"Invalid status {new_status!r}.")
        inc = self.get_incident(incident_id)
        old_status = inc.status
        inc.status = new_status
        msg = f"Status changed from '{old_status}' to '{new_status}'"
        if note:
            msg += f" — {note}"
        evt = IncidentEvent.new(
            incident_id,
            EventType.STATUS_CHANGE.value,
            actor,
            msg,
            {"old_status": old_status, "new_status": new_status},
        )
        inc.timeline.append(evt)
        with sqlite3.connect(self.db_path) as conn:
            self._save_incident(conn, inc)
            self._save_event(conn, evt)
        return inc

    def add_timeline_entry(
        self,
        incident_id: str,
        event_type: str,
        message: str,
        actor: str,
        metadata: Optional[dict] = None,
    ) -> IncidentEvent:
        """Append a free-form event to an incident's timeline."""
        self.get_incident(incident_id)  # validate exists
        evt = IncidentEvent.new(incident_id, event_type, actor, message, metadata or {})
        with sqlite3.connect(self.db_path) as conn:
            self._save_event(conn, evt)
        return evt

    def assign(self, incident_id: str, assignee: str) -> Incident:
        """Assign (or re-assign) an incident to a responder."""
        inc = self.get_incident(incident_id)
        old = inc.assignee
        inc.assignee = assignee
        evt = IncidentEvent.new(
            incident_id,
            EventType.ACTION_TAKEN.value,
            "system",
            f"Assigned to {assignee} (was: {old or 'unassigned'})",
            {"old_assignee": old, "new_assignee": assignee},
        )
        inc.timeline.append(evt)
        with sqlite3.connect(self.db_path) as conn:
            self._save_incident(conn, inc)
            self._save_event(conn, evt)
        return inc

    def escalate(
        self, incident_id: str, new_severity: str, reason: str, actor: str
    ) -> Incident:
        """Escalate an incident to a higher severity level."""
        if new_severity not in [s.value for s in IncidentSeverity]:
            raise ValueError(f"Invalid severity {new_severity!r}.")
        inc = self.get_incident(incident_id)
        old_severity = inc.severity
        inc.severity = new_severity
        evt = IncidentEvent.new(
            incident_id,
            EventType.ESCALATION.value,
            actor,
            f"Escalated from {old_severity} to {new_severity}. Reason: {reason}",
            {"old_severity": old_severity, "new_severity": new_severity, "reason": reason},
        )
        inc.timeline.append(evt)
        with sqlite3.connect(self.db_path) as conn:
            self._save_incident(conn, inc)
            self._save_event(conn, evt)
        return inc

    def resolve(
        self, incident_id: str, resolution_summary: str, actor: str
    ) -> Incident:
        """Mark an incident as resolved and compute MTTR."""
        inc = self.get_incident(incident_id)
        inc.status = IncidentStatus.RESOLVED.value
        inc.resolved_at = datetime.now(timezone.utc).isoformat()
        inc.impact_summary = resolution_summary
        evt = IncidentEvent.new(
            incident_id,
            EventType.STATUS_CHANGE.value,
            actor,
            f"Resolved in {inc.mttr_minutes:.1f} min. Summary: {resolution_summary}",
            {"resolution_summary": resolution_summary, "mttr_minutes": inc.mttr_minutes},
        )
        inc.timeline.append(evt)
        with sqlite3.connect(self.db_path) as conn:
            self._save_incident(conn, inc)
            self._save_event(conn, evt)
        return inc

    def get_incident(self, incident_id: str) -> Incident:
        """Fetch a single incident by its UUID."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id, title, description, severity, status, affected_services, "
                "assignee, created_at, resolved_at, tags, impact_summary, runbook_url "
                "FROM incidents WHERE id = ?",
                (incident_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Incident not found: {incident_id!r}")
            events = self._load_events(conn, incident_id)
        return self._row_to_incident(row, events)

    def list_incidents(
        self,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        service: Optional[str] = None,
        limit: int = 50,
    ) -> list:
        """List incidents with optional filters, newest first."""
        sql = (
            "SELECT id, title, description, severity, status, affected_services, "
            "assignee, created_at, resolved_at, tags, impact_summary, runbook_url "
            "FROM incidents WHERE 1=1"
        )
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if severity:
            sql += " AND severity = ?"
            params.append(severity)
        if service:
            sql += " AND affected_services LIKE ?"
            params.append(f"%{service}%")
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
            return [
                self._row_to_incident(row, self._load_events(conn, row[0]))
                for row in rows
            ]

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def calculate_mttr(
        self, service: Optional[str] = None, days: int = 30
    ) -> dict:
        """
        Calculate mean time to resolution grouped by severity and service.

        Returns avg / min / max MTTR for each severity level, plus a per-service
        breakdown, all scoped to the last *days* days.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        sql = (
            "SELECT severity, affected_services, created_at, resolved_at "
            "FROM incidents WHERE resolved_at IS NOT NULL AND created_at >= ?"
        )
        params: list = [cutoff]
        if service:
            sql += " AND affected_services LIKE ?"
            params.append(f"%{service}%")
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()

        by_severity: dict[str, list] = {s.value: [] for s in IncidentSeverity}
        by_service: dict[str, list] = {}
        for sev, svc_json, created_at, resolved_at in rows:
            created = datetime.fromisoformat(created_at)
            resolved = datetime.fromisoformat(resolved_at)
            mttr = (resolved - created).total_seconds() / 60
            by_severity[sev].append(mttr)
            for svc in json.loads(svc_json or "[]"):
                by_service.setdefault(svc, []).append(mttr)

        def _stats(vals: list) -> dict:
            if not vals:
                return {
                    "count": 0,
                    "avg_mttr_mins": None,
                    "min_mttr_mins": None,
                    "max_mttr_mins": None,
                }
            return {
                "count": len(vals),
                "avg_mttr_mins": round(sum(vals) / len(vals), 2),
                "min_mttr_mins": round(min(vals), 2),
                "max_mttr_mins": round(max(vals), 2),
            }

        return {
            "period_days": days,
            "total_resolved": sum(len(v) for v in by_severity.values()),
            "by_severity": {sev: _stats(vals) for sev, vals in by_severity.items()},
            "by_service": {
                svc: {"count": len(v), "avg_mttr_mins": round(sum(v) / len(v), 2)}
                for svc, v in by_service.items()
            },
        }

    def check_sla_breach(self, incident_id: str) -> dict:
        """
        Evaluate whether an incident has breached its SLA targets.

        Checks both response SLA (time from creation to first action)
        and resolution SLA (total time to resolve).
        """
        inc = self.get_incident(incident_id)
        policy = self.sla_policies.get(inc.severity)
        if policy is None:
            return {"error": f"No SLA policy defined for severity {inc.severity}"}

        now = datetime.now(timezone.utc)
        created = datetime.fromisoformat(inc.created_at)
        elapsed_mins = (now - created).total_seconds() / 60

        if inc.resolved_at:
            resolved = datetime.fromisoformat(inc.resolved_at)
            resolution_elapsed = (resolved - created).total_seconds() / 60
        else:
            resolution_elapsed = elapsed_mins

        response_breached = elapsed_mins > policy.response_time_mins
        resolution_breached = resolution_elapsed > policy.resolution_time_mins

        return {
            "incident_id": incident_id,
            "severity": inc.severity,
            "status": inc.status,
            "elapsed_mins": round(elapsed_mins, 2),
            "resolution_elapsed_mins": round(resolution_elapsed, 2),
            "response_sla_mins": policy.response_time_mins,
            "resolution_sla_mins": policy.resolution_time_mins,
            "response_breached": response_breached,
            "resolution_breached": resolution_breached,
            "any_breach": response_breached or resolution_breached,
        }

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def postmortem_template(self, incident_id: str) -> str:
        """Generate a filled-in markdown postmortem skeleton."""
        inc = self.get_incident(incident_id)
        sla = self.check_sla_breach(incident_id)
        sla_label = "⚠️ SLA BREACHED" if sla.get("any_breach") else "✅ Within SLA"
        services = ", ".join(inc.affected_services) if inc.affected_services else "N/A"
        mttr = f"{inc.mttr_minutes:.1f} minutes" if inc.mttr_minutes is not None else "Not yet resolved"
        resolved_str = (inc.resolved_at[:19] + "Z") if inc.resolved_at else "*(pending)*"
        tl_md = "\n".join(
            f"- **{e.timestamp[:19]}Z** `[{e.event_type}]` **{e.actor}:** {e.message}"
            for e in inc.timeline
        ) or "*(No events recorded)*"

        return f"""# Post-Mortem: {inc.title}

**Incident ID:** `{inc.id}`
**Severity:** {inc.severity} | **Status:** {inc.status}
**Date:** {inc.created_at[:10]} | **MTTR:** {mttr}
**SLA Status:** {sla_label}

---

## Summary

> *(2–3 sentence overview: what happened, customer impact, how it was resolved.)*

**Affected Services:** {services}
**Impact Summary:** {inc.impact_summary or "*(To be completed)*"}

---

## Timeline

{tl_md}

---

## Root Cause

**Primary Root Cause:** *(e.g., Memory leak in auth-service caused by unbounded cache growth)*

---

## Contributing Factors

- *(Factor 1: e.g., No memory limits set in Kubernetes deployment)*
- *(Factor 2: e.g., Alert threshold was set too high, delaying detection by 12 min)*
- *(Factor 3: e.g., No runbook existed for this failure mode)*

---

## Detection & Response

| Phase | Time | Notes |
|-------|------|-------|
| Incident started | {inc.created_at[:19]}Z | |
| First detected | *(fill in)* | |
| On-call paged | *(fill in)* | |
| Mitigation started | *(fill in)* | |
| Incident resolved | {resolved_str} | |

---

## Impact Assessment

| Dimension | Details |
|-----------|---------|
| Users affected | *(fill in)* |
| Services degraded | {services} |
| Data loss | *(fill in)* |
| Financial impact | *(fill in)* |
| SLA breach | {sla_label} |

---

## Action Items

| # | Action | Owner | Due Date | Priority |
|---|--------|-------|----------|----------|
| 1 | *(e.g., Add memory limits to all services)* | *(team)* | *(date)* | High |
| 2 | *(e.g., Lower alert threshold to 70% memory)* | *(team)* | *(date)* | Medium |
| 3 | *(e.g., Write runbook for cache overflow)* | *(team)* | *(date)* | Medium |

---

## Lessons Learned

### What Went Well
- *(e.g., On-call responded within SLA)*
- *(e.g., Rollback procedure was well-documented)*

### What Could Be Improved
- *(e.g., Alert thresholds need regular tuning)*
- *(e.g., Runbook was missing for this scenario)*

---

## References

- Runbook: {inc.runbook_url or "*(not linked)*"}
- Incident Record: `{inc.id}`
- Tags: {", ".join(inc.tags) if inc.tags else "none"}

---

*Owner: {inc.assignee or "unassigned"} | Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d")}*
"""

    def export_report(self, incident_id: str, format: str = "markdown") -> str:
        """Export a full incident report as markdown or JSON."""
        inc = self.get_incident(incident_id)
        sla = self.check_sla_breach(incident_id)

        if format == "json":
            return json.dumps(
                {
                    "id": inc.id,
                    "title": inc.title,
                    "description": inc.description,
                    "severity": inc.severity,
                    "status": inc.status,
                    "affected_services": inc.affected_services,
                    "assignee": inc.assignee,
                    "created_at": inc.created_at,
                    "resolved_at": inc.resolved_at,
                    "mttr_minutes": inc.mttr_minutes,
                    "tags": inc.tags,
                    "impact_summary": inc.impact_summary,
                    "runbook_url": inc.runbook_url,
                    "sla_breach": sla,
                    "timeline": [
                        {
                            "id": e.id,
                            "event_type": e.event_type,
                            "actor": e.actor,
                            "message": e.message,
                            "timestamp": e.timestamp,
                            "metadata": e.metadata,
                        }
                        for e in inc.timeline
                    ],
                },
                indent=2,
            )

        # --- Markdown ---
        services = ", ".join(inc.affected_services) if inc.affected_services else "N/A"
        mttr = f"{inc.mttr_minutes:.1f} min" if inc.mttr_minutes is not None else "N/A"
        sla_label = "⚠️ BREACHED" if sla.get("any_breach") else "✅ OK"
        resolved_str = (inc.resolved_at[:19] + "Z") if inc.resolved_at else "Pending"
        tl_rows = "\n".join(
            f"| {e.timestamp[:19]}Z | `{e.event_type}` | {e.actor} | {e.message} |"
            for e in inc.timeline
        )
        return f"""# Incident Report: {inc.title}

| Field | Value |
|-------|-------|
| **ID** | `{inc.id}` |
| **Severity** | {inc.severity} |
| **Status** | {inc.status} |
| **Assignee** | {inc.assignee or "Unassigned"} |
| **Created** | {inc.created_at[:19]}Z |
| **Resolved** | {resolved_str} |
| **MTTR** | {mttr} |
| **Services** | {services} |
| **SLA** | {sla_label} |

## Description

{inc.description or "*(No description provided)*"}

## Impact Summary

{inc.impact_summary or "*(Pending)*"}

## Timeline

| Time | Type | Actor | Message |
|------|------|-------|---------|
{tl_rows or "| — | — | — | No events |"}

## SLA Details

| Metric | Value |
|--------|-------|
| Response SLA | {sla.get("response_sla_mins")} min |
| Resolution SLA | {sla.get("resolution_sla_mins")} min |
| Elapsed | {sla.get("elapsed_mins")} min |
| Response Breached | {"⚠️ Yes" if sla.get("response_breached") else "✅ No"} |
| Resolution Breached | {"⚠️ Yes" if sla.get("resolution_breached") else "✅ No"} |
"""

    def get_dashboard(self) -> dict:
        """Return a real-time snapshot of incident health metrics."""
        with sqlite3.connect(self.db_path) as conn:
            open_by_sev: dict[str, int] = {}
            for sev in IncidentSeverity:
                count = conn.execute(
                    "SELECT COUNT(*) FROM incidents "
                    "WHERE severity = ? AND status NOT IN ('resolved', 'closed')",
                    (sev.value,),
                ).fetchone()[0]
                open_by_sev[sev.value] = count

            total_open = conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE status NOT IN ('resolved', 'closed')"
            ).fetchone()[0]
            total_resolved = conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE status IN ('resolved', 'closed')"
            ).fetchone()[0]
            recent = conn.execute(
                "SELECT id, title, severity, status, created_at "
                "FROM incidents ORDER BY created_at DESC LIMIT 5"
            ).fetchall()

        # SLA breach count across all active incidents
        active = self.list_incidents()
        active = [
            i
            for i in active
            if i.status not in (IncidentStatus.RESOLVED.value, IncidentStatus.CLOSED.value)
        ]
        breach_count = sum(
            1 for i in active if self.check_sla_breach(i.id).get("any_breach")
        )

        return {
            "open_by_severity": open_by_sev,
            "total_open": total_open,
            "total_resolved": total_resolved,
            "sla_breach_count": breach_count,
            "mttr_stats": self.calculate_mttr(days=30),
            "recent_incidents": [
                {
                    "id": r[0],
                    "title": r[1],
                    "severity": r[2],
                    "status": r[3],
                    "created_at": r[4],
                }
                for r in recent
            ],
        }


# ---------------------------------------------------------------------------
# Rich display helpers
# ---------------------------------------------------------------------------


def _sev_icon(sev: str) -> str:
    return {"P1": "🔴", "P2": "🟠", "P3": "🟡", "P4": "🟢"}.get(sev, "⚪")


def _status_icon(status: str) -> str:
    return {
        "open": "🆕",
        "investigating": "🔍",
        "mitigating": "🛡️",
        "resolved": "✅",
        "closed": "📁",
    }.get(status, "❓")


def print_incident(inc: Incident) -> None:
    if not HAS_RICH or _console is None:
        print(f"[{inc.severity}] {inc.title} ({inc.status}) — {inc.id}")
        return
    sev_color = SEVERITY_COLORS.get(inc.severity, "white")
    status_color = STATUS_COLORS.get(inc.status, "white")
    panel_title = (
        f"[{sev_color}]{_sev_icon(inc.severity)} {inc.severity}[/] "
        f"— [{status_color}]{_status_icon(inc.status)} {inc.status}[/]"
    )
    services = ", ".join(inc.affected_services) if inc.affected_services else "N/A"
    mttr = f"{inc.mttr_minutes:.1f} min" if inc.mttr_minutes is not None else "ongoing"
    raw_color = sev_color.split()[-1]
    body = (
        f"[bold white]{inc.title}[/bold white]\n"
        f"[dim]{inc.id}[/dim]\n\n"
        f"[cyan]Services :[/cyan] {services}\n"
        f"[cyan]Assignee :[/cyan] {inc.assignee or 'Unassigned'}\n"
        f"[cyan]MTTR     :[/cyan] {mttr}\n"
        f"[cyan]Created  :[/cyan] {inc.created_at[:19]}Z\n"
        f"[cyan]Desc     :[/cyan] {inc.description or 'N/A'}"
    )
    _console.print(Panel(body, title=panel_title, border_style=raw_color))


def print_incident_table(incidents: list) -> None:
    if not HAS_RICH or _console is None:
        for inc in incidents:
            print(f"[{inc.severity}] [{inc.status}] {inc.title[:50]} — {inc.id[:8]}")
        return
    table = Table(title="📋 Incidents", box=box.ROUNDED, header_style="bold cyan")
    table.add_column("ID", style="dim", width=10)
    table.add_column("Sev", width=6)
    table.add_column("Status", width=14)
    table.add_column("Title")
    table.add_column("Assignee", width=15)
    table.add_column("Services", width=22)
    table.add_column("Created", width=18)
    for inc in incidents:
        sc = SEVERITY_COLORS.get(inc.severity, "white")
        stc = STATUS_COLORS.get(inc.status, "white")
        table.add_row(
            inc.id[:8] + "…",
            f"[{sc}]{inc.severity}[/]",
            f"[{stc}]{inc.status}[/]",
            inc.title[:45],
            inc.assignee or "—",
            ", ".join(inc.affected_services[:2]),
            inc.created_at[:16] + "Z",
        )
    _console.print(table)


def print_timeline(inc: Incident) -> None:
    if not HAS_RICH or _console is None:
        for e in inc.timeline:
            print(f"  {e.timestamp[:19]}Z  [{e.event_type}]  {e.actor}: {e.message}")
        return
    type_colors = {
        "status_change": "yellow",
        "note": "blue",
        "escalation": "red",
        "action_taken": "green",
        "external_link": "cyan",
        "page_sent": "magenta",
    }
    table = Table(
        title=f"🕒 Timeline: {inc.title}", box=box.SIMPLE, header_style="bold cyan"
    )
    table.add_column("Time", style="dim", width=21)
    table.add_column("Type", width=16)
    table.add_column("Actor", width=15)
    table.add_column("Message")
    for e in inc.timeline:
        tc = type_colors.get(e.event_type, "white")
        table.add_row(
            e.timestamp[:19] + "Z",
            f"[{tc}]{e.event_type}[/]",
            e.actor,
            e.message,
        )
    _console.print(table)


def print_dashboard(data: dict) -> None:
    if not HAS_RICH or _console is None:
        print(json.dumps(data, indent=2))
        return
    _console.print("\n[bold cyan]🚨 BlackRoad Incident Dashboard[/bold cyan]\n")
    sev_table = Table(title="Open Incidents by Severity", box=box.ROUNDED)
    sev_table.add_column("Severity")
    sev_table.add_column("Open", justify="right")
    for sev, count in data["open_by_severity"].items():
        color = SEVERITY_COLORS.get(sev, "white")
        sev_table.add_row(f"[{color}]{_sev_icon(sev)} {sev}[/]", str(count))
    _console.print(sev_table)
    _console.print(
        f"\n[bold]Total Open:[/] {data['total_open']}  "
        f"[bold]Resolved:[/] {data['total_resolved']}  "
        f"[bold red]SLA Breaches:[/] {data['sla_breach_count']}\n"
    )
    if data["recent_incidents"]:
        rec = Table(title="Recent Incidents", box=box.SIMPLE)
        rec.add_column("ID", style="dim", width=10)
        rec.add_column("Sev", width=6)
        rec.add_column("Status", width=14)
        rec.add_column("Title")
        rec.add_column("Created", width=18)
        for r in data["recent_incidents"]:
            sc = SEVERITY_COLORS.get(r["severity"], "white")
            stc = STATUS_COLORS.get(r["status"], "white")
            rec.add_row(
                r["id"][:8] + "…",
                f"[{sc}]{r['severity']}[/]",
                f"[{stc}]{r['status']}[/]",
                r["title"][:50],
                r["created_at"][:16] + "Z",
            )
        _console.print(rec)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="incident-manager",
        description="BlackRoad Incident Manager — production incident tracking",
    )
    parser.add_argument("--db", help="Custom SQLite database path", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    # create
    p = sub.add_parser("create", help="Create a new incident")
    p.add_argument("title")
    p.add_argument("severity", choices=["P1", "P2", "P3", "P4"])
    p.add_argument("--services", nargs="+", default=[])
    p.add_argument("--description", default="")
    p.add_argument("--assignee", default=None)

    # update
    p = sub.add_parser("update", help="Update incident status")
    p.add_argument("incident_id")
    p.add_argument(
        "status",
        choices=["open", "investigating", "mitigating", "resolved", "closed"],
    )
    p.add_argument("--note", default="")
    p.add_argument("--actor", default="cli-user")

    # resolve
    p = sub.add_parser("resolve", help="Resolve an incident")
    p.add_argument("incident_id")
    p.add_argument("summary")
    p.add_argument("--actor", default="cli-user")

    # timeline
    p = sub.add_parser("timeline", help="Show incident timeline")
    p.add_argument("incident_id")

    # list
    p = sub.add_parser("list", help="List incidents")
    p.add_argument("--status", default=None)
    p.add_argument("--severity", default=None)
    p.add_argument("--service", default=None)
    p.add_argument("--limit", type=int, default=20)

    # report
    p = sub.add_parser("report", help="Export incident report")
    p.add_argument("incident_id")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")

    # postmortem
    p = sub.add_parser("postmortem", help="Generate postmortem template")
    p.add_argument("incident_id")

    # dashboard
    sub.add_parser("dashboard", help="Show incident dashboard")

    # mttr
    p = sub.add_parser("mttr", help="Show MTTR statistics")
    p.add_argument("--service", default=None)
    p.add_argument("--days", type=int, default=30)

    # escalate
    p = sub.add_parser("escalate", help="Escalate incident severity")
    p.add_argument("incident_id")
    p.add_argument("severity", choices=["P1", "P2", "P3", "P4"])
    p.add_argument("reason")
    p.add_argument("--actor", default="cli-user")

    # assign
    p = sub.add_parser("assign", help="Assign incident to a responder")
    p.add_argument("incident_id")
    p.add_argument("assignee")

    # note
    p = sub.add_parser("note", help="Add a note to the timeline")
    p.add_argument("incident_id")
    p.add_argument("message")
    p.add_argument("--actor", default="cli-user")

    # sla
    p = sub.add_parser("sla", help="Check SLA breach status")
    p.add_argument("incident_id")

    return parser


def _resolve_id(mgr: IncidentManager, id_prefix: str) -> str:
    """Resolve a full incident ID from a short prefix."""
    incidents = mgr.list_incidents(limit=1000)
    matches = [i for i in incidents if i.id.startswith(id_prefix)]
    if not matches:
        raise ValueError(f"No incident found with ID prefix: {id_prefix!r}")
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous prefix {id_prefix!r} matches {len(matches)} incidents"
        )
    return matches[0].id


def main() -> None:
    parser = _get_parser()
    args = parser.parse_args()
    mgr = IncidentManager(db_path=args.db)

    try:
        if args.command == "create":
            inc = mgr.create_incident(
                args.title,
                args.severity,
                args.services,
                description=args.description,
                assignee=args.assignee,
            )
            print_incident(inc)
            if not HAS_RICH:
                print(f"Created: {inc.id}")

        elif args.command == "update":
            inc = mgr.update_status(
                _resolve_id(mgr, args.incident_id),
                args.status,
                note=args.note,
                actor=args.actor,
            )
            print_incident(inc)

        elif args.command == "resolve":
            inc = mgr.resolve(
                _resolve_id(mgr, args.incident_id), args.summary, actor=args.actor
            )
            print_incident(inc)

        elif args.command == "timeline":
            inc = mgr.get_incident(_resolve_id(mgr, args.incident_id))
            print_timeline(inc)

        elif args.command == "list":
            incidents = mgr.list_incidents(
                status=args.status,
                severity=args.severity,
                service=args.service,
                limit=args.limit,
            )
            print_incident_table(incidents)

        elif args.command == "report":
            print(
                mgr.export_report(
                    _resolve_id(mgr, args.incident_id), format=args.format
                )
            )

        elif args.command == "postmortem":
            print(mgr.postmortem_template(_resolve_id(mgr, args.incident_id)))

        elif args.command == "dashboard":
            print_dashboard(mgr.get_dashboard())

        elif args.command == "mttr":
            data = mgr.calculate_mttr(service=args.service, days=args.days)
            if HAS_RICH and _console:
                _console.print_json(json.dumps(data))
            else:
                print(json.dumps(data, indent=2))

        elif args.command == "escalate":
            inc = mgr.escalate(
                _resolve_id(mgr, args.incident_id),
                args.severity,
                args.reason,
                actor=args.actor,
            )
            print_incident(inc)

        elif args.command == "assign":
            inc = mgr.assign(_resolve_id(mgr, args.incident_id), args.assignee)
            print_incident(inc)

        elif args.command == "note":
            evt = mgr.add_timeline_entry(
                _resolve_id(mgr, args.incident_id),
                EventType.NOTE.value,
                args.message,
                actor=args.actor,
            )
            if HAS_RICH and _console:
                _console.print(f"[green]✓[/] Note added: {evt.id}")
            else:
                print(f"Note added: {evt.id}")

        elif args.command == "sla":
            result = mgr.check_sla_breach(_resolve_id(mgr, args.incident_id))
            if HAS_RICH and _console:
                label = "⚠️ SLA BREACHED" if result.get("any_breach") else "✅ Within SLA"
                color = "red" if result.get("any_breach") else "green"
                _console.print(f"[{color}]{label}[/]")
                _console.print_json(json.dumps(result))
            else:
                print(json.dumps(result, indent=2))

    except ValueError as exc:
        if HAS_RICH and _console:
            _console.print(f"[bold red]Error:[/] {exc}", err=True)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
