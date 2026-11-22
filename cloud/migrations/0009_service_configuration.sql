CREATE TABLE service_configuration (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  revision INTEGER NOT NULL DEFAULT 0,
  active TEXT,
  previous TEXT,
  staged TEXT,
  test_id TEXT,
  tested_at INTEGER,
  staged_health TEXT,
  active_at INTEGER,
  health TEXT,
  health_started_at INTEGER
);

INSERT INTO service_configuration (singleton) VALUES (1);
