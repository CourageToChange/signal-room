CREATE UNIQUE INDEX idx_incidents_active_fingerprint
  ON incidents(fingerprint)
  WHERE state IN ('open', 'recovering');
CREATE UNIQUE INDEX idx_incident_events_uuid ON incident_events(event_uuid);
CREATE INDEX idx_incidents_cursor ON incidents(opened_at DESC, id DESC);
CREATE INDEX idx_incident_events_incident_cursor ON incident_events(incident_id, id DESC);
CREATE INDEX idx_samples_retention ON samples(observed_at);
CREATE INDEX idx_rollups_retention ON hourly_rollups(bucket_at);

CREATE TRIGGER incident_events_immutable_update
BEFORE UPDATE ON incident_events
BEGIN
  SELECT RAISE(ABORT, 'incident events are immutable');
END;

CREATE TRIGGER audit_events_immutable_update
BEFORE UPDATE ON audit_events
BEGIN
  SELECT RAISE(ABORT, 'audit events are immutable');
END;
