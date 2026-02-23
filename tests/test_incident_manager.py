"""Tests for BlackRoad Incident Manager."""

from __future__ import annotations

import json
import time
import uuid

import pytest

from incident_manager import (
    EventType,
    Incident,
    IncidentEvent,
    IncidentManager,
    IncidentSeverity,
    IncidentStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path):
    """Fresh IncidentManager backed by a temp SQLite database."""
    return IncidentManager(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def sample_incident(manager):
    """A persisted P2 incident with two affected services."""
    return manager.create_incident(
        title="Database connection pool exhausted",
        severity="P2",
        affected_services=["payments", "checkout"],
        description="Connection pool at 100%, queries timing out",
        assignee="alice",
    )


# ---------------------------------------------------------------------------
# Incident Creation
# ---------------------------------------------------------------------------


class TestIncidentCreation:
    def test_create_basic_incident(self, manager):
        inc = manager.create_incident("API down", "P1", ["api-gateway"])
        assert inc.id
        assert inc.title == "API down"
        assert inc.severity == IncidentSeverity.P1.value
        assert inc.status == IncidentStatus.OPEN.value
        assert "api-gateway" in inc.affected_services

    def test_create_with_all_fields(self, manager):
        inc = manager.create_incident(
            title="High error rate",
            severity="P2",
            affected_services=["auth", "api"],
            description="5xx rate at 40%",
            assignee="bob",
        )
        assert inc.assignee == "bob"
        assert inc.description == "5xx rate at 40%"
        assert len(inc.affected_services) == 2

    def test_create_incident_has_initial_timeline_event(self, manager):
        inc = manager.create_incident("Test", "P3", ["svc"])
        assert len(inc.timeline) >= 1
        assert inc.timeline[0].event_type == EventType.STATUS_CHANGE.value

    def test_create_invalid_severity_raises(self, manager):
        with pytest.raises(ValueError, match="Invalid severity"):
            manager.create_incident("Bad sev", "P5", ["svc"])

    def test_create_incident_persisted(self, manager):
        inc = manager.create_incident("Persist test", "P4", ["svc-a"])
        fetched = manager.get_incident(inc.id)
        assert fetched.id == inc.id
        assert fetched.title == inc.title
        assert fetched.severity == inc.severity

    def test_incident_id_is_uuid(self, manager):
        inc = manager.create_incident("UUID test", "P1", [])
        uuid.UUID(inc.id)  # raises ValueError if not a valid UUID

    def test_create_incident_created_at_is_utc(self, manager):
        from datetime import timezone

        inc = manager.create_incident("Time test", "P3", [])
        dt = __import__("datetime").datetime.fromisoformat(inc.created_at)
        assert dt.tzinfo is not None

    def test_create_multiple_affected_services(self, manager):
        svcs = ["api", "auth", "billing", "notifications"]
        inc = manager.create_incident("Multi-svc", "P1", svcs)
        assert inc.affected_services == svcs

    def test_get_nonexistent_incident_raises(self, manager):
        with pytest.raises(ValueError, match="not found"):
            manager.get_incident(str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Status Transitions
# ---------------------------------------------------------------------------


class TestStatusTransitions:
    def test_update_status_investigating(self, manager, sample_incident):
        updated = manager.update_status(sample_incident.id, "investigating")
        assert updated.status == IncidentStatus.INVESTIGATING.value

    def test_update_status_adds_timeline_event(self, manager, sample_incident):
        manager.update_status(
            sample_incident.id, "mitigating", note="Patch applied", actor="charlie"
        )
        inc = manager.get_incident(sample_incident.id)
        status_events = [
            e for e in inc.timeline if e.event_type == EventType.STATUS_CHANGE.value
        ]
        assert len(status_events) >= 2  # creation + update

    def test_update_status_invalid_raises(self, manager, sample_incident):
        with pytest.raises(ValueError, match="Invalid status"):
            manager.update_status(sample_incident.id, "unknown_status")

    def test_update_status_records_actor(self, manager, sample_incident):
        manager.update_status(
            sample_incident.id, "investigating", actor="sre-on-call"
        )
        inc = manager.get_incident(sample_incident.id)
        actors = [e.actor for e in inc.timeline]
        assert "sre-on-call" in actors

    def test_full_lifecycle(self, manager):
        inc = manager.create_incident("Full lifecycle", "P1", ["core"])
        inc = manager.update_status(inc.id, "investigating", actor="eng1")
        assert inc.status == "investigating"
        inc = manager.update_status(inc.id, "mitigating", actor="eng1")
        assert inc.status == "mitigating"
        inc = manager.resolve(inc.id, "Fixed by rolling restart", actor="eng1")
        assert inc.status == IncidentStatus.RESOLVED.value
        assert inc.resolved_at is not None


# ---------------------------------------------------------------------------
# Timeline Tracking
# ---------------------------------------------------------------------------


class TestTimelineTracking:
    def test_add_note(self, manager, sample_incident):
        evt = manager.add_timeline_entry(
            sample_incident.id, EventType.NOTE.value, "Checked DB metrics", "ops-team"
        )
        assert evt.id
        assert evt.event_type == EventType.NOTE.value
        assert evt.message == "Checked DB metrics"

    def test_add_page_sent(self, manager, sample_incident):
        evt = manager.add_timeline_entry(
            sample_incident.id,
            EventType.PAGE_SENT.value,
            "Paged on-call engineer",
            "pagerduty",
            metadata={"oncall": "alice", "channel": "sms"},
        )
        assert evt.metadata["oncall"] == "alice"
        assert evt.metadata["channel"] == "sms"

    def test_timeline_ordered_by_time(self, manager, sample_incident):
        manager.add_timeline_entry(sample_incident.id, "note", "First", "user")
        time.sleep(0.01)
        manager.add_timeline_entry(sample_incident.id, "note", "Second", "user")
        inc = manager.get_incident(sample_incident.id)
        timestamps = [e.timestamp for e in inc.timeline]
        assert timestamps == sorted(timestamps)

    def test_add_external_link(self, manager, sample_incident):
        evt = manager.add_timeline_entry(
            sample_incident.id,
            EventType.EXTERNAL_LINK.value,
            "Datadog dashboard",
            "alice",
            metadata={"url": "https://app.datadoghq.com/dash/123"},
        )
        assert "url" in evt.metadata

    def test_add_action_taken(self, manager, sample_incident):
        evt = manager.add_timeline_entry(
            sample_incident.id,
            EventType.ACTION_TAKEN.value,
            "Restarted auth-service pod",
            "sre",
        )
        assert evt.event_type == EventType.ACTION_TAKEN.value

    def test_timeline_persisted_after_reload(self, manager, sample_incident):
        manager.add_timeline_entry(
            sample_incident.id, "note", "Persistent note", "tester"
        )
        reloaded = manager.get_incident(sample_incident.id)
        messages = [e.message for e in reloaded.timeline]
        assert "Persistent note" in messages


# ---------------------------------------------------------------------------
# MTTR Calculation
# ---------------------------------------------------------------------------


class TestMTTRCalculation:
    def test_mttr_none_for_open_incident(self, manager, sample_incident):
        assert sample_incident.mttr_minutes is None

    def test_mttr_calculated_on_resolve(self, manager):
        inc = manager.create_incident("MTTR test", "P2", ["api"])
        time.sleep(0.05)
        resolved = manager.resolve(inc.id, "Fixed", actor="test")
        assert resolved.mttr_minutes is not None
        assert resolved.mttr_minutes >= 0

    def test_calculate_mttr_by_severity(self, manager):
        for sev in ["P1", "P2", "P3"]:
            inc = manager.create_incident(f"MTTR {sev}", sev, ["svc"])
            manager.resolve(inc.id, "Resolved", actor="test")
        stats = manager.calculate_mttr()
        for sev in ["P1", "P2", "P3"]:
            assert stats["by_severity"][sev]["count"] == 1
            assert stats["by_severity"][sev]["avg_mttr_mins"] is not None

    def test_calculate_mttr_empty_returns_none(self, manager):
        stats = manager.calculate_mttr()
        for sev in IncidentSeverity:
            assert stats["by_severity"][sev.value]["count"] == 0
            assert stats["by_severity"][sev.value]["avg_mttr_mins"] is None

    def test_calculate_mttr_by_service(self, manager):
        inc = manager.create_incident("Svc MTTR", "P2", ["my-service"])
        manager.resolve(inc.id, "Fixed", actor="test")
        stats = manager.calculate_mttr()
        assert "my-service" in stats["by_service"]
        assert stats["by_service"]["my-service"]["count"] == 1

    def test_mttr_total_resolved_count(self, manager):
        for _ in range(3):
            inc = manager.create_incident("Bulk", "P4", ["x"])
            manager.resolve(inc.id, "Done", actor="bot")
        stats = manager.calculate_mttr()
        assert stats["total_resolved"] == 3


# ---------------------------------------------------------------------------
# SLA Breach Detection
# ---------------------------------------------------------------------------


class TestSLABreach:
    def test_p1_sla_policy(self, manager):
        inc = manager.create_incident("P1 test", "P1", ["core"])
        result = manager.check_sla_breach(inc.id)
        assert result["response_sla_mins"] == 15
        assert result["resolution_sla_mins"] == 60

    def test_p4_sla_policy(self, manager):
        inc = manager.create_incident("P4 test", "P4", ["logs"])
        result = manager.check_sla_breach(inc.id)
        assert result["response_sla_mins"] == 480
        assert result["resolution_sla_mins"] == 4320

    def test_sla_not_breached_immediately_for_p4(self, manager):
        inc = manager.create_incident("Fresh P4", "P4", ["logs"])
        result = manager.check_sla_breach(inc.id)
        assert not result["response_breached"]
        assert not result["resolution_breached"]
        assert not result["any_breach"]

    def test_sla_breach_result_structure(self, manager):
        inc = manager.create_incident("SLA struct", "P1", ["core"])
        result = manager.check_sla_breach(inc.id)
        required_keys = {
            "incident_id",
            "severity",
            "status",
            "elapsed_mins",
            "response_sla_mins",
            "resolution_sla_mins",
            "response_breached",
            "resolution_breached",
            "any_breach",
        }
        assert required_keys.issubset(result.keys())

    def test_sla_breach_incident_id_matches(self, manager):
        inc = manager.create_incident("ID match", "P2", ["api"])
        result = manager.check_sla_breach(inc.id)
        assert result["incident_id"] == inc.id

    def test_resolved_incident_sla_uses_resolution_time(self, manager):
        inc = manager.create_incident("Resolved SLA", "P4", ["svc"])
        manager.resolve(inc.id, "Fast fix", actor="test")
        result = manager.check_sla_breach(inc.id)
        # Resolved immediately — well within P4 4320-min window
        assert not result["resolution_breached"]


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------


class TestEscalation:
    def test_escalate_severity(self, manager):
        inc = manager.create_incident("Escalation test", "P3", ["api"])
        escalated = manager.escalate(inc.id, "P1", "Customer data at risk", actor="cto")
        assert escalated.severity == "P1"

    def test_escalate_adds_timeline_event(self, manager):
        inc = manager.create_incident("Esc timeline", "P3", ["api"])
        manager.escalate(inc.id, "P2", "Impact expanding", actor="sre")
        inc = manager.get_incident(inc.id)
        esc_events = [e for e in inc.timeline if e.event_type == EventType.ESCALATION.value]
        assert len(esc_events) == 1
        assert "P3" in esc_events[0].message
        assert "P2" in esc_events[0].message

    def test_escalate_invalid_severity_raises(self, manager):
        inc = manager.create_incident("Bad esc", "P3", ["svc"])
        with pytest.raises(ValueError, match="Invalid severity"):
            manager.escalate(inc.id, "P0", "very bad", actor="x")

    def test_escalate_metadata_stored(self, manager):
        inc = manager.create_incident("Meta esc", "P4", ["svc"])
        manager.escalate(inc.id, "P2", "Spike", actor="sre")
        inc = manager.get_incident(inc.id)
        esc = next(e for e in inc.timeline if e.event_type == EventType.ESCALATION.value)
        assert esc.metadata["old_severity"] == "P4"
        assert esc.metadata["new_severity"] == "P2"


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


class TestAssignment:
    def test_assign_incident(self, manager):
        inc = manager.create_incident("Assign me", "P3", ["svc"])
        assigned = manager.assign(inc.id, "bob")
        assert assigned.assignee == "bob"

    def test_reassign_incident(self, manager, sample_incident):
        reassigned = manager.assign(sample_incident.id, "charlie")
        assert reassigned.assignee == "charlie"

    def test_assign_adds_timeline_event(self, manager):
        inc = manager.create_incident("Assign tl", "P2", ["api"])
        manager.assign(inc.id, "diana")
        inc = manager.get_incident(inc.id)
        assign_events = [
            e for e in inc.timeline if e.event_type == EventType.ACTION_TAKEN.value
        ]
        assert any("diana" in e.message for e in assign_events)


# ---------------------------------------------------------------------------
# Postmortem Generation
# ---------------------------------------------------------------------------


class TestPostmortem:
    def test_postmortem_contains_required_sections(self, manager, sample_incident):
        pm = manager.postmortem_template(sample_incident.id)
        for section in ["Summary", "Timeline", "Root Cause", "Action Items", "Lessons Learned"]:
            assert section in pm

    def test_postmortem_contains_incident_id(self, manager, sample_incident):
        pm = manager.postmortem_template(sample_incident.id)
        assert sample_incident.id in pm

    def test_postmortem_contains_title(self, manager, sample_incident):
        pm = manager.postmortem_template(sample_incident.id)
        assert sample_incident.title in pm

    def test_postmortem_contains_severity(self, manager, sample_incident):
        pm = manager.postmortem_template(sample_incident.id)
        assert "P2" in pm

    def test_postmortem_shows_sla_status(self, manager, sample_incident):
        pm = manager.postmortem_template(sample_incident.id)
        assert "SLA" in pm

    def test_postmortem_shows_resolved_mttr(self, manager):
        inc = manager.create_incident("PM resolved", "P1", ["core"])
        manager.resolve(inc.id, "Fixed fast", actor="eng")
        pm = manager.postmortem_template(inc.id)
        assert "minutes" in pm


# ---------------------------------------------------------------------------
# Report Export
# ---------------------------------------------------------------------------


class TestReportExport:
    def test_export_markdown_structure(self, manager, sample_incident):
        report = manager.export_report(sample_incident.id, format="markdown")
        assert "# Incident Report:" in report
        assert sample_incident.title in report
        assert "SLA" in report
        assert "Timeline" in report

    def test_export_json_valid(self, manager, sample_incident):
        raw = manager.export_report(sample_incident.id, format="json")
        data = json.loads(raw)
        assert data["id"] == sample_incident.id
        assert data["severity"] == sample_incident.severity
        assert "timeline" in data
        assert "sla_breach" in data

    def test_export_json_has_mttr_after_resolve(self, manager):
        inc = manager.create_incident("Export MTTR", "P2", ["api"])
        manager.resolve(inc.id, "Done", actor="test")
        data = json.loads(manager.export_report(inc.id, format="json"))
        assert data["mttr_minutes"] is not None
        assert data["mttr_minutes"] >= 0

    def test_export_json_timeline_entries(self, manager, sample_incident):
        manager.add_timeline_entry(
            sample_incident.id, "note", "Extra note", "tester"
        )
        data = json.loads(manager.export_report(sample_incident.id, format="json"))
        messages = [e["message"] for e in data["timeline"]]
        assert any("Extra note" in m for m in messages)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class TestDashboard:
    def test_dashboard_keys(self, manager):
        dash = manager.get_dashboard()
        for key in [
            "open_by_severity",
            "total_open",
            "total_resolved",
            "sla_breach_count",
            "mttr_stats",
            "recent_incidents",
        ]:
            assert key in dash

    def test_dashboard_counts_open(self, manager):
        manager.create_incident("Open P1", "P1", ["api"])
        manager.create_incident("Open P2", "P2", ["db"])
        inc = manager.create_incident("Resolved P3", "P3", ["cache"])
        manager.resolve(inc.id, "Fixed", actor="test")
        dash = manager.get_dashboard()
        assert dash["total_open"] == 2
        assert dash["total_resolved"] == 1
        assert dash["open_by_severity"]["P1"] == 1
        assert dash["open_by_severity"]["P2"] == 1

    def test_dashboard_recent_incidents_list(self, manager):
        for i in range(3):
            manager.create_incident(f"Incident {i}", "P3", ["svc"])
        dash = manager.get_dashboard()
        assert len(dash["recent_incidents"]) == 3

    def test_dashboard_empty_state(self, manager):
        dash = manager.get_dashboard()
        assert dash["total_open"] == 0
        assert dash["total_resolved"] == 0
        assert dash["sla_breach_count"] == 0


# ---------------------------------------------------------------------------
# List & Filter
# ---------------------------------------------------------------------------


class TestListAndFilter:
    def test_list_all(self, manager):
        manager.create_incident("A", "P1", ["svc-a"])
        manager.create_incident("B", "P2", ["svc-b"])
        incidents = manager.list_incidents()
        assert len(incidents) == 2

    def test_filter_by_severity(self, manager):
        manager.create_incident("P1 only", "P1", ["core"])
        manager.create_incident("P4 only", "P4", ["logs"])
        p1s = manager.list_incidents(severity="P1")
        assert all(i.severity == "P1" for i in p1s)
        assert len(p1s) == 1

    def test_filter_by_status(self, manager):
        inc = manager.create_incident("Open one", "P2", ["api"])
        manager.update_status(inc.id, "investigating")
        open_incs = manager.list_incidents(status="open")
        assert all(i.status == "open" for i in open_incs)

    def test_filter_by_service(self, manager):
        manager.create_incident("Auth inc", "P2", ["auth-service"])
        manager.create_incident("DB inc", "P2", ["database"])
        results = manager.list_incidents(service="auth-service")
        assert all("auth-service" in i.affected_services for i in results)

    def test_list_limit(self, manager):
        for i in range(10):
            manager.create_incident(f"Inc {i}", "P4", ["svc"])
        results = manager.list_incidents(limit=3)
        assert len(results) == 3

    def test_list_newest_first(self, manager):
        for i in range(3):
            manager.create_incident(f"Inc {i}", "P3", ["svc"])
            time.sleep(0.01)
        results = manager.list_incidents()
        timestamps = [i.created_at for i in results]
        assert timestamps == sorted(timestamps, reverse=True)
