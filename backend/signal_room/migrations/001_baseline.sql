CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  kind TEXT NOT NULL,
  parent_id TEXT REFERENCES assets(id),
  runbook_id TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS asset_state (
  asset_id TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
  health TEXT NOT NULL DEFAULT 'unknown',
  last_observed_at TEXT,
  unhealthy_since_at TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  consecutive_successes INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT 'Awaiting first observation',
  latency_ms REAL,
  cpu_ratio REAL,
  memory_ratio REAL,
  disk_ratio REAL
);

CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  observed_at TEXT NOT NULL,
  health TEXT NOT NULL,
  message TEXT NOT NULL,
  latency_ms REAL,
  cpu_ratio REAL,
  memory_ratio REAL,
  disk_ratio REAL,
  details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_samples_asset_time ON samples(asset_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY,
  root_asset_id TEXT NOT NULL REFERENCES assets(id),
  severity TEXT NOT NULL,
  state TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  acknowledged_at TEXT,
  acknowledged_by TEXT,
  recovered_at TEXT,
  closed_at TEXT,
  closed_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_state_opened ON incidents(state, opened_at DESC);

CREATE TABLE IF NOT EXISTS incident_assets (
  incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  asset_id TEXT NOT NULL REFERENCES assets(id),
  PRIMARY KEY (incident_id, asset_id)
);

CREATE TABLE IF NOT EXISTS incident_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_incident_events_id ON incident_events(id);

CREATE TABLE IF NOT EXISTS incident_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  author TEXT NOT NULL,
  body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
