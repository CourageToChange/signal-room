ALTER TABLE notification_outbox RENAME TO notification_outbox_v3;
DROP INDEX idx_outbox_due;

CREATE TABLE notification_outbox (
  event_uuid TEXT PRIMARY KEY,
  incident_id TEXT REFERENCES incidents(id) ON DELETE SET NULL,
  event_kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  next_attempt_at TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  delivered_at TEXT,
  dead_letter_at TEXT,
  suppressed_at TEXT,
  diagnostic TEXT
);

INSERT INTO notification_outbox(
  event_uuid, incident_id, event_kind, payload_json, created_at,
  next_attempt_at, attempt_count, delivered_at, dead_letter_at, diagnostic
)
SELECT event_uuid, incident_id, event_kind, payload_json, created_at,
       next_attempt_at, attempt_count, delivered_at, dead_letter_at, diagnostic
FROM notification_outbox_v3;

DROP TABLE notification_outbox_v3;

CREATE INDEX idx_outbox_due ON notification_outbox(next_attempt_at)
  WHERE delivered_at IS NULL AND dead_letter_at IS NULL AND suppressed_at IS NULL;

INSERT INTO audit_events(
  event_uuid, created_at, kind, subject_type, subject_id,
  actor_subject, actor_email, message, metadata_json
)
SELECT
  'migration-' || lower(hex(randomblob(16))),
  strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
  'notifications_suppressed',
  'notification_outbox',
  NULL,
  NULL,
  NULL,
  'Undelivered notifications suppressed because delivery is disabled',
  printf('{"count":%d}', COUNT(*))
FROM notification_outbox
WHERE delivered_at IS NULL
  AND dead_letter_at IS NULL
  AND suppressed_at IS NULL
  AND EXISTS (
    SELECT 1 FROM runtime_state
    WHERE key='notification_enabled' AND lower(value)='false'
  )
HAVING COUNT(*) > 0;

UPDATE notification_outbox
SET suppressed_at=strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
    diagnostic='notifications_disabled'
WHERE delivered_at IS NULL
  AND dead_letter_at IS NULL
  AND suppressed_at IS NULL
  AND EXISTS (
    SELECT 1 FROM runtime_state
    WHERE key='notification_enabled' AND lower(value)='false'
  );
