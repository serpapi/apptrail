-- Initial schema. Keep this migration unchanged after release.

CREATE TABLE apps (
	id INTEGER NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	aliases JSON NOT NULL, 
	website TEXT NOT NULL, 
	archived BOOLEAN NOT NULL, 
	created_at FLOAT NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE candidates (
	token VARCHAR(64) NOT NULL, 
	data JSON NOT NULL, 
	expires_at FLOAT NOT NULL, 
	PRIMARY KEY (token)
);

CREATE TABLE monitors (
	id INTEGER NOT NULL, 
	signature VARCHAR(64) NOT NULL, 
	"query" TEXT NOT NULL, 
	source VARCHAR(40) NOT NULL, 
	country VARCHAR(2) NOT NULL, 
	language VARCHAR(16) NOT NULL, 
	device VARCHAR(20) NOT NULL, 
	depth INTEGER NOT NULL, 
	frequency VARCHAR(20) NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	next_run_at FLOAT NOT NULL, 
	created_at FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (signature)
);

CREATE TABLE settings (
	"key" VARCHAR(60) NOT NULL, 
	value JSON NOT NULL, 
	PRIMARY KEY ("key")
);

CREATE TABLE listings (
	id INTEGER NOT NULL, 
	app_id INTEGER NOT NULL, 
	platform VARCHAR(20) NOT NULL, 
	external_id VARCHAR(200) NOT NULL, 
	bundle_id VARCHAR(200) NOT NULL, 
	title TEXT NOT NULL, 
	url TEXT NOT NULL, 
	icon TEXT NOT NULL, 
	developer TEXT NOT NULL, 
	country VARCHAR(2) NOT NULL, 
	language VARCHAR(16) NOT NULL, 
	metadata_json JSON NOT NULL, 
	verified_at FLOAT NOT NULL, 
	next_refresh_at FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (platform, external_id), 
	UNIQUE (app_id, platform), 
	FOREIGN KEY(app_id) REFERENCES apps (id) ON DELETE CASCADE
);

CREATE TABLE targets (
	monitor_id INTEGER NOT NULL, 
	app_id INTEGER NOT NULL, 
	PRIMARY KEY (monitor_id, app_id), 
	FOREIGN KEY(monitor_id) REFERENCES monitors (id) ON DELETE CASCADE, 
	FOREIGN KEY(app_id) REFERENCES apps (id) ON DELETE CASCADE
);

CREATE TABLE profiles (
	id INTEGER NOT NULL, 
	listing_id INTEGER NOT NULL, 
	checked_at FLOAT NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(listing_id) REFERENCES listings (id) ON DELETE CASCADE
);

CREATE INDEX ix_profiles_listing_id ON profiles (listing_id);

CREATE TABLE runs (
	id INTEGER NOT NULL, 
	monitor_id INTEGER, 
	listing_id INTEGER, 
	kind VARCHAR(20) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	params JSON NOT NULL, 
	result JSON NOT NULL, 
	responses JSON NOT NULL, 
	error TEXT NOT NULL, 
	attempts INTEGER NOT NULL, 
	requests_count INTEGER NOT NULL, 
	created_at FLOAT NOT NULL, 
	available_at FLOAT NOT NULL, 
	started_at FLOAT, 
	finished_at FLOAT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(monitor_id) REFERENCES monitors (id) ON DELETE SET NULL, 
	FOREIGN KEY(listing_id) REFERENCES listings (id) ON DELETE SET NULL
);

CREATE INDEX ix_runs_created_at ON runs (created_at);

CREATE INDEX ix_runs_monitor_id ON runs (monitor_id);

CREATE INDEX ix_runs_status ON runs (status);

CREATE TABLE observations (
	run_id INTEGER NOT NULL, 
	app_id INTEGER NOT NULL, 
	data JSON NOT NULL, 
	retrospective BOOLEAN NOT NULL, 
	PRIMARY KEY (run_id, app_id), 
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE, 
	FOREIGN KEY(app_id) REFERENCES apps (id) ON DELETE CASCADE
);
