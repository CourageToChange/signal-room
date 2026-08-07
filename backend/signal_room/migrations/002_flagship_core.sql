ALTER TABLE assets ADD COLUMN retired_at TEXT;
ALTER TABLE assets ADD COLUMN configuration_revision TEXT NOT NULL DEFAULT 'legacy';

CREATE TABLE asset_dependencies (
  asset_id TEXT NOT NULL REFERENCES assets(id),
  depends_on_id TEXT NOT NULL REFERENCES assets(id),
  PRIMARY KEY (asset_id, depends_on_id),
  CHECK (asset_id != depends_on_id)
);

CREATE TABLE asset_checks (
  asset_id TEXT NOT NULL REFERENCES assets(id),
  check_id TEXT NOT NULL,
  check_type TEXT NOT NULL,
  definition_json TEXT NOT NULL,
  PRIMARY KEY (asset_id, check_id)
);

ALTER TABLE asset_state ADD COLUMN last_check_id TEXT;
CREATE TABLE check_state (
  asset_id TEXT NOT NULL REFERENCES assets(id),
  check_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  health TEXT NOT NULL DEFAULT 'unknown',
  last_observed_at TEXT,
  unhealthy_since_at TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  consecutive_successes INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT 'Awaiting first observation',
  latency_ms REAL,
  cpu_ratio REAL,
  memory_ratio REAL,
  disk_ratio REAL,
  condition TEXT,
  PRIMARY KEY (asset_id, check_id),
  FOREIGN KEY (asset_id, check_id) REFERENCES asset_checks(asset_id, check_id)
);
ALTER TABLE samples ADD COLUMN check_id TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE samples ADD COLUMN provider TEXT NOT NULL DEFAULT 'fixture';
ALTER TABLE samples ADD COLUMN provider_run_id TEXT;
ALTER TABLE samples ADD COLUMN condition TEXT;

ALTER TABLE incidents ADD COLUMN previous_incident_id TEXT REFERENCES incidents(id);
ALTER TABLE incidents ADD COLUMN fingerprint TEXT;
ALTER TABLE incidents ADD COLUMN incident_type TEXT NOT NULL DEFAULT 'asset_down';
ALTER TABLE incidents ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
UPDATE incidents SET fingerprint = incident_type || ':' || root_asset_id WHERE fingerprint IS NULL;

ALTER TABLE incident_events ADD COLUMN event_uuid TEXT;
ALTER TABLE incident_events ADD COLUMN actor_subject TEXT;
ALTER TABLE incident_events ADD COLUMN actor_email TEXT;
UPDATE incident_events SET event_uuid = printf('legacy-%d', id) WHERE event_uuid IS NULL;

CREATE TABLE provider_runs (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  attempted_at TEXT NOT NULL,
  completed_at TEXT,
  success INTEGER NOT NULL DEFAULT 0 CHECK (success IN (0, 1)),
  observation_count INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_provider_runs_provider_attempt ON provider_runs(provider, attempted_at DESC);

CREATE TABLE provider_state (
  provider TEXT PRIMARY KEY,
  last_attempt_at TEXT,
  last_success_at TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT 'Provider has not run'
);

CREATE TABLE hourly_rollups (
  asset_id TEXT NOT NULL REFERENCES assets(id),
  bucket_at TEXT NOT NULL,
  sample_count INTEGER NOT NULL,
  healthy_count INTEGER NOT NULL,
  cpu_ratio_avg REAL,
  memory_ratio_avg REAL,
  disk_ratio_avg REAL,
  latency_ms_avg REAL,
  PRIMARY KEY (asset_id, bucket_at)
);
CREATE INDEX idx_rollups_asset_time ON hourly_rollups(asset_id, bucket_at DESC);

CREATE TABLE maintenance_windows (
  id TEXT PRIMARY KEY,
  starts_at TEXT NOT NULL,
  ends_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL,
  cancelled_at TEXT,
  cancelled_by TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  CHECK (ends_at > starts_at)
);

CREATE TABLE maintenance_assets (
  maintenance_id TEXT NOT NULL REFERENCES maintenance_windows(id),
  asset_id TEXT NOT NULL REFERENCES assets(id),
  PRIMARY KEY (maintenance_id, asset_id)
);

CREATE TABLE idempotency_records (
  actor_subject TEXT NOT NULL,
  operation TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response_json TEXT NOT NULL,
  status_code INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  PRIMARY KEY (actor_subject, operation, idempotency_key)
);

CREATE TABLE notification_outbox (
  event_uuid TEXT PRIMARY KEY,
  incident_id TEXT REFERENCES incidents(id),
  event_kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  next_attempt_at TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  delivered_at TEXT,
  dead_letter_at TEXT,
  diagnostic TEXT
);
CREATE INDEX idx_outbox_due ON notification_outbox(next_attempt_at)
  WHERE delivered_at IS NULL AND dead_letter_at IS NULL;

CREATE TABLE audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_uuid TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id TEXT,
  actor_subject TEXT,
  actor_email TEXT,
  message TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_audit_events_created ON audit_events(created_at DESC);

CREATE TABLE stream_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_uuid TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  topic TEXT NOT NULL,
  kind TEXT NOT NULL,
  subject_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_stream_events_created ON stream_events(created_at DESC);

DROP TABLE IF EXISTS schema_version;
