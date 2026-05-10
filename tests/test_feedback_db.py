"""
test_feedback_db.py — Tests for the self-learning feedback database.

Tests cover:
    - Recording feedback (CRUD)
    - Suppression rule auto-generation from patterns
    - should_suppress() logic
    - Pending event flow (store → resolve)
    - Statistics
    - Rule management (toggle, delete)
"""

import os
import sys
import time
import tempfile
import pytest

# Add services/dashboard to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "dashboard"))

from feedback_db import FeedbackDB, FeedbackRecord


@pytest.fixture
def db(tmp_path):
    """Create a fresh FeedbackDB in a temp directory for each test."""
    db_path = os.path.join(str(tmp_path), "test_feedback.db")
    return FeedbackDB(db_path)


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------
class TestFeedbackCRUD:
    def test_record_and_retrieve(self, db):
        """Can record a feedback entry and retrieve it by event_id."""
        record = FeedbackRecord(
            event_id="1708472312-0",
            verdict="false_alarm",
            event_type="person_appeared",
            zone="Driveway",
            time_period="morning",
            action="standing",
            confidence=0.72,
        )
        row_id = db.record_feedback(record)
        assert row_id > 0

        retrieved = db.get_feedback("1708472312-0")
        assert retrieved is not None
        assert retrieved["verdict"] == "false_alarm"
        assert retrieved["zone"] == "Driveway"
        assert retrieved["time_period"] == "morning"

    def test_record_replaces_on_duplicate_event_id(self, db):
        """INSERT OR REPLACE should update existing record."""
        record1 = FeedbackRecord(
            event_id="test-001",
            verdict="pending",
            event_type="person_appeared",
        )
        db.record_feedback(record1)

        record2 = FeedbackRecord(
            event_id="test-001",
            verdict="real_detection",
            event_type="person_appeared",
        )
        db.record_feedback(record2)

        retrieved = db.get_feedback("test-001")
        assert retrieved["verdict"] == "real_detection"

    def test_get_recent_feedback(self, db):
        """Recent feedback returns newest first, limited by count."""
        for i in range(10):
            db.record_feedback(FeedbackRecord(
                event_id=f"event-{i:03d}",
                verdict="false_alarm",
                event_type="person_appeared",
                timestamp=time.time() + i,
            ))

        recent = db.get_recent_feedback(limit=5)
        assert len(recent) == 5
        # Newest should be first
        assert recent[0]["event_id"] == "event-009"

    def test_get_nonexistent_feedback(self, db):
        """Getting feedback for non-existent event returns None."""
        assert db.get_feedback("nonexistent") is None


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
class TestStats:
    def test_empty_stats(self, db):
        """Stats on empty DB should return zeros."""
        stats = db.get_stats()
        assert stats["total_feedback"] == 0
        assert stats["active_suppression_rules"] == 0
        assert stats["alert_accuracy"] == 0.0

    def test_stats_with_data(self, db):
        """Stats should correctly count verdicts and compute accuracy."""
        for i in range(3):
            db.record_feedback(FeedbackRecord(
                event_id=f"real-{i}", verdict="real_detection",
                event_type="person_appeared",
            ))
        for i in range(7):
            db.record_feedback(FeedbackRecord(
                event_id=f"false-{i}", verdict="false_alarm",
                event_type="person_appeared",
            ))

        stats = db.get_stats()
        assert stats["total_feedback"] == 10
        assert stats["by_verdict"]["real_detection"] == 3
        assert stats["by_verdict"]["false_alarm"] == 7
        assert stats["alert_accuracy"] == 0.3  # 3/10


# ---------------------------------------------------------------------------
# Suppression Rules — Auto-generation
# ---------------------------------------------------------------------------
class TestAutoRules:
    def test_identity_rule_created_after_threshold(self, db):
        """After IDENTITY_THRESHOLD false alarms for the same identity,
        an identity suppression rule should be auto-created."""
        threshold = db.IDENTITY_THRESHOLD

        for i in range(threshold):
            db.record_feedback(FeedbackRecord(
                event_id=f"mail-{i}",
                verdict="false_alarm",
                event_type="person_appeared",
                identity_label="Mail Carrier",
            ))

        rules = db.get_suppression_rules()
        identity_rules = [r for r in rules if r["rule_type"] == "identity"]
        assert len(identity_rules) == 1
        assert identity_rules[0]["identity"] == "Mail Carrier"
        assert identity_rules[0]["active"] == 1

    def test_identity_rule_not_created_below_threshold(self, db):
        """Below threshold, no rule should be created."""
        for i in range(db.IDENTITY_THRESHOLD - 1):
            db.record_feedback(FeedbackRecord(
                event_id=f"mail-{i}",
                verdict="false_alarm",
                event_type="person_appeared",
                identity_label="Mail Carrier",
            ))

        rules = db.get_suppression_rules()
        assert len(rules) == 0

    def test_zone_time_rule_created(self, db):
        """After ZONE_TIME_THRESHOLD false alarms for same zone+time,
        a zone_time suppression rule should be created."""
        threshold = db.ZONE_TIME_THRESHOLD

        for i in range(threshold):
            db.record_feedback(FeedbackRecord(
                event_id=f"driveway-am-{i}",
                verdict="false_alarm",
                event_type="person_appeared",
                zone="Driveway",
                time_period="morning",
            ))

        rules = db.get_suppression_rules()
        zt_rules = [r for r in rules if r["rule_type"] == "zone_time"]
        assert len(zt_rules) == 1
        assert zt_rules[0]["zone"] == "Driveway"
        assert zt_rules[0]["time_period"] == "morning"

    def test_no_rule_for_real_detections(self, db):
        """Real detection verdicts should never create suppression rules."""
        for i in range(10):
            db.record_feedback(FeedbackRecord(
                event_id=f"detection-{i}",
                verdict="real_detection",
                event_type="person_appeared",
                identity_label="Intruder",
                zone="Backyard",
                time_period="night",
            ))

        rules = db.get_suppression_rules()
        assert len(rules) == 0

    def test_no_duplicate_rules(self, db):
        """Exceeding threshold multiple times should not create duplicate rules."""
        for i in range(db.IDENTITY_THRESHOLD * 3):
            db.record_feedback(FeedbackRecord(
                event_id=f"mail-{i}",
                verdict="false_alarm",
                event_type="person_appeared",
                identity_label="Mail Carrier",
            ))

        rules = db.get_suppression_rules()
        identity_rules = [r for r in rules if r["rule_type"] == "identity"]
        assert len(identity_rules) == 1


# ---------------------------------------------------------------------------
# Suppression Checks
# ---------------------------------------------------------------------------
class TestShouldSuppress:
    def test_suppress_by_identity(self, db):
        """Should suppress when identity matches an active rule."""
        # Create rule by exceeding threshold
        for i in range(db.IDENTITY_THRESHOLD):
            db.record_feedback(FeedbackRecord(
                event_id=f"mail-{i}",
                verdict="false_alarm",
                event_type="person_appeared",
                identity_label="Mail Carrier",
            ))

        assert db.should_suppress(identity="Mail Carrier") is True
        assert db.should_suppress(identity="Unknown Person") is False

    def test_suppress_case_insensitive(self, db):
        """Identity matching should be case-insensitive."""
        for i in range(db.IDENTITY_THRESHOLD):
            db.record_feedback(FeedbackRecord(
                event_id=f"mail-{i}",
                verdict="false_alarm",
                event_type="person_appeared",
                identity_label="Mail Carrier",
            ))

        assert db.should_suppress(identity="mail carrier") is True
        assert db.should_suppress(identity="MAIL CARRIER") is True

    def test_suppress_by_zone_time(self, db):
        """Should suppress when zone+time matches an active rule."""
        for i in range(db.ZONE_TIME_THRESHOLD):
            db.record_feedback(FeedbackRecord(
                event_id=f"driveway-am-{i}",
                verdict="false_alarm",
                event_type="person_appeared",
                zone="Driveway",
                time_period="morning",
            ))

        assert db.should_suppress(zone="Driveway", time_period="morning") is True
        assert db.should_suppress(zone="Driveway", time_period="night") is False
        assert db.should_suppress(zone="Backyard", time_period="morning") is False

    def test_no_suppression_when_no_rules(self, db):
        """Should not suppress when there are no rules."""
        assert db.should_suppress(identity="Someone", zone="Somewhere") is False

    def test_disabled_rule_not_suppressed(self, db):
        """Disabled rules should not suppress."""
        for i in range(db.IDENTITY_THRESHOLD):
            db.record_feedback(FeedbackRecord(
                event_id=f"mail-{i}",
                verdict="false_alarm",
                event_type="person_appeared",
                identity_label="Mail Carrier",
            ))

        # Rule exists and is active
        assert db.should_suppress(identity="Mail Carrier") is True

        # Disable the rule
        rules = db.get_suppression_rules()
        db.toggle_rule(rules[0]["id"], active=False)

        # Should no longer suppress
        assert db.should_suppress(identity="Mail Carrier") is False


# ---------------------------------------------------------------------------
# Pending Events (Telegram button flow)
# ---------------------------------------------------------------------------
class TestPendingEvents:
    def test_store_and_resolve_pending(self, db):
        """Store a pending event, then resolve it with a verdict."""
        db.store_pending_event(
            event_id="1708472312-0",
            event_type="person_appeared",
            telegram_message_id=12345,
            zone="Driveway",
            time_period="morning",
        )

        # Should be pending
        record = db.get_feedback("1708472312-0")
        assert record is not None
        assert record["verdict"] == "pending"
        assert record["telegram_message_id"] == 12345

        # Resolve it
        ok = db.resolve_pending("1708472312-0", "false_alarm")
        assert ok is True

        # Should now be false_alarm
        record = db.get_feedback("1708472312-0")
        assert record["verdict"] == "false_alarm"

    def test_resolve_with_identity(self, db):
        """Resolve pending with an identity label."""
        db.store_pending_event(
            event_id="test-001",
            event_type="person_appeared",
            telegram_message_id=999,
        )

        db.resolve_pending("test-001", "identified", identity_label="Neighbor Dave")

        record = db.get_feedback("test-001")
        assert record["verdict"] == "identified"
        assert record["identity_label"] == "Neighbor Dave"

    def test_resolve_nonexistent_creates_record(self, db):
        """Resolving a non-existent event should upsert a new feedback record."""
        ok = db.resolve_pending("nonexistent", "real_detection")
        assert ok is True
        # Verify the record was created
        record = db.get_feedback("nonexistent")
        assert record is not None
        assert record["verdict"] == "real_detection"

    def test_lookup_by_telegram_message_id(self, db):
        """Can look up pending events by Telegram message ID."""
        db.store_pending_event(
            event_id="test-001",
            event_type="person_appeared",
            telegram_message_id=12345,
        )

        record = db.get_pending_by_message_id(12345)
        assert record is not None
        assert record["event_id"] == "test-001"

        assert db.get_pending_by_message_id(99999) is None


# ---------------------------------------------------------------------------
# Rule Management
# ---------------------------------------------------------------------------
class TestRuleManagement:
    def _create_identity_rule(self, db):
        """Helper: create an identity suppression rule."""
        for i in range(db.IDENTITY_THRESHOLD):
            db.record_feedback(FeedbackRecord(
                event_id=f"mail-{i}",
                verdict="false_alarm",
                event_type="person_appeared",
                identity_label="Mail Carrier",
            ))
        rules = db.get_suppression_rules()
        return rules[0]

    def test_toggle_rule(self, db):
        rule = self._create_identity_rule(db)
        assert rule["active"] == 1

        db.toggle_rule(rule["id"], active=False)
        rules = db.get_suppression_rules()
        assert rules[0]["active"] == 0

        db.toggle_rule(rule["id"], active=True)
        rules = db.get_suppression_rules()
        assert rules[0]["active"] == 1

    def test_delete_rule(self, db):
        rule = self._create_identity_rule(db)
        db.delete_rule(rule["id"])
        rules = db.get_suppression_rules()
        assert len(rules) == 0
