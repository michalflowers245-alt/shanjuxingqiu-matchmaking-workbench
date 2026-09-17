CREATE UNIQUE INDEX IF NOT EXISTS ux_xhs_extraction_workspace_id
  ON xhs_post_extractions(workspace_id,id);

CREATE TABLE IF NOT EXISTS xhs_archive_versions (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  extraction_id TEXT NOT NULL,
  post_id TEXT NOT NULL,
  observed_post_id TEXT NOT NULL,
  archive_path TEXT NOT NULL,
  body_raw TEXT,
  body_normalized TEXT,
  body_source TEXT NOT NULL,
  body_evidence_json TEXT NOT NULL DEFAULT '{}',
  archive_status TEXT NOT NULL DEFAULT 'PARTIAL',
  verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
  content_sha256 TEXT,
  manifest_sha256 TEXT,
  captured_at TEXT NOT NULL,
  collector_version TEXT NOT NULL,
  UNIQUE(workspace_id,id),
  FOREIGN KEY(workspace_id,extraction_id) REFERENCES xhs_post_extractions(workspace_id,id),
  CHECK(post_id=observed_post_id)
);

CREATE TABLE IF NOT EXISTS xhs_post_assets (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  archive_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('carousel','cover','video','audio','media_part','ocr_frame')),
  ordinal INTEGER NOT NULL CHECK(ordinal>=1),
  source_ref TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_host TEXT NOT NULL DEFAULT '',
  rendition TEXT NOT NULL DEFAULT 'browser_returned',
  mime_declared TEXT,
  mime_detected TEXT,
  extension TEXT,
  width INTEGER,
  height INTEGER,
  duration_ms INTEGER,
  byte_size INTEGER CHECK(byte_size>=0),
  bytes_expected INTEGER,
  sha256 TEXT,
  relative_path TEXT,
  status TEXT NOT NULL DEFAULT 'DISCOVERED',
  http_status INTEGER,
  error_code TEXT,
  retry_after TEXT,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(workspace_id,id),
  UNIQUE(archive_id,role,ordinal),
  UNIQUE(workspace_id,archive_id,id),
  FOREIGN KEY(workspace_id,archive_id) REFERENCES xhs_archive_versions(workspace_id,id)
);

CREATE TABLE IF NOT EXISTS xhs_transcripts (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  archive_id TEXT NOT NULL,
  asset_id TEXT,
  source_type TEXT NOT NULL CHECK(source_type IN ('asr','platform_subtitle','frame_ocr')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  language TEXT,
  duration_ms INTEGER,
  engine TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  model_name TEXT,
  model_sha256 TEXT,
  source_sha256 TEXT,
  parameters_json TEXT NOT NULL DEFAULT '{}',
  text_raw TEXT NOT NULL DEFAULT '',
  segments_json TEXT NOT NULL DEFAULT '[]',
  confidence_json TEXT NOT NULL DEFAULT '{}',
  low_confidence_json TEXT NOT NULL DEFAULT '[]',
  output_files_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'QUEUED',
  review_status TEXT NOT NULL DEFAULT 'UNREVIEWED',
  coverage_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(archive_id,source_type,revision),
  FOREIGN KEY(workspace_id,archive_id) REFERENCES xhs_archive_versions(workspace_id,id),
  FOREIGN KEY(workspace_id,archive_id,asset_id) REFERENCES xhs_post_assets(workspace_id,archive_id,id)
);

CREATE TABLE IF NOT EXISTS xhs_archive_jobs (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  extraction_id TEXT NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN ('archive','transcribe','verify')),
  idempotency_key TEXT NOT NULL,
  browser_profile_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'QUEUED',
  stage TEXT NOT NULL DEFAULT 'QUEUED',
  progress_json TEXT NOT NULL DEFAULT '{}',
  attempt INTEGER NOT NULL DEFAULT 0,
  retry_after TEXT,
  deadline_at TEXT,
  lease_owner TEXT,
  lease_expires_at TEXT,
  heartbeat_at TEXT,
  error_code TEXT,
  revision INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,idempotency_key),
  FOREIGN KEY(workspace_id,extraction_id) REFERENCES xhs_post_extractions(workspace_id,id)
);

CREATE INDEX IF NOT EXISTS ix_xhs_jobs_queue
  ON xhs_archive_jobs(status,retry_after,created_at);

CREATE TABLE IF NOT EXISTS xhs_browser_pause (
  profile_id TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  retry_after TEXT,
  requires_user_action INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
