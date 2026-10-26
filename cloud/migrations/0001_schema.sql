PRAGMA foreign_keys=ON;
CREATE TABLE accounts(id TEXT PRIMARY KEY,username TEXT NOT NULL,username_norm TEXT NOT NULL UNIQUE,auth_verifier TEXT NOT NULL,auth_salt TEXT NOT NULL,email TEXT,email_verified_at INTEGER,created_at INTEGER NOT NULL,quota_bytes INTEGER NOT NULL CHECK(quota_bytes>0),allocated_bytes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE auth_challenges(id TEXT PRIMARY KEY,username_norm TEXT NOT NULL,purpose TEXT NOT NULL,salt TEXT NOT NULL,account_id TEXT NOT NULL,expires_at INTEGER NOT NULL,consumed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE devices(id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),name TEXT NOT NULL,platform TEXT NOT NULL,created_at INTEGER NOT NULL,last_seen_at INTEGER NOT NULL,revoked_at INTEGER);
CREATE INDEX devices_account ON devices(account_id,revoked_at);
CREATE INDEX devices_recent ON devices(account_id,created_at DESC);
CREATE INDEX devices_revoked ON devices(revoked_at);
CREATE TRIGGER device_limit BEFORE INSERT ON devices WHEN (SELECT COUNT(*) FROM devices WHERE account_id=NEW.account_id AND revoked_at IS NULL)>=20 BEGIN SELECT RAISE(ABORT,'DEVICE_LIMIT'); END;
CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),device_id TEXT NOT NULL REFERENCES devices(id),expires_at INTEGER NOT NULL);
CREATE INDEX sessions_device ON sessions(device_id);
CREATE TABLE rates(key TEXT PRIMARY KEY,n INTEGER NOT NULL,expires_at INTEGER NOT NULL);
CREATE TABLE email_challenges(id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),email TEXT NOT NULL,code_hash TEXT NOT NULL,expires_at INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,sent INTEGER NOT NULL DEFAULT 0,consumed INTEGER NOT NULL DEFAULT 0);
CREATE TRIGGER bind_email AFTER UPDATE OF consumed ON email_challenges WHEN NEW.consumed=1 AND OLD.consumed=0 BEGIN
 UPDATE accounts SET email=NEW.email,email_verified_at=unixepoch()*1000 WHERE id=NEW.account_id;
END;
CREATE TABLE limits(id INTEGER PRIMARY KEY CHECK(id=1),bytes_limit INTEGER NOT NULL,allocated_bytes INTEGER NOT NULL DEFAULT 0);
INSERT INTO limits(id,bytes_limit) VALUES(1,8589934592);
CREATE TABLE allocations(id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),size INTEGER NOT NULL CHECK(size>=0));
CREATE INDEX allocation_account ON allocations(account_id);
CREATE TRIGGER reserve_quota BEFORE INSERT ON allocations BEGIN
 SELECT CASE WHEN (SELECT NEW.size+allocated_bytes>quota_bytes FROM accounts WHERE id=NEW.account_id) THEN RAISE(ABORT,'ACCOUNT_QUOTA') END;
 SELECT CASE WHEN (SELECT NEW.size+allocated_bytes>bytes_limit FROM limits WHERE id=1) THEN RAISE(ABORT,'GLOBAL_QUOTA') END;
END;
CREATE TRIGGER allocate_bytes AFTER INSERT ON allocations BEGIN UPDATE accounts SET allocated_bytes=allocated_bytes+NEW.size WHERE id=NEW.account_id;UPDATE limits SET allocated_bytes=allocated_bytes+NEW.size WHERE id=1;END;
CREATE TRIGGER free_bytes AFTER DELETE ON allocations BEGIN UPDATE accounts SET allocated_bytes=allocated_bytes-OLD.size WHERE id=OLD.account_id;UPDATE limits SET allocated_bytes=allocated_bytes-OLD.size WHERE id=1;END;
CREATE TABLE budgets(period TEXT NOT NULL,kind TEXT NOT NULL,n INTEGER NOT NULL,PRIMARY KEY(period,kind));
CREATE TABLE uploads(id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),item_id TEXT NOT NULL,base_version INTEGER NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,mime_type TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,chunk_size INTEGER NOT NULL,part_count INTEGER NOT NULL,state TEXT NOT NULL CHECK(state IN ('pending','verifying','committed','garbage')),expires_at INTEGER NOT NULL,lease TEXT,lease_until INTEGER NOT NULL DEFAULT 0,garbage_after INTEGER NOT NULL DEFAULT 0,next_part INTEGER NOT NULL DEFAULT 0,verified_size INTEGER NOT NULL DEFAULT 0,hash_state TEXT,computed_sha256 TEXT);
CREATE TABLE read_leases(upload_id TEXT NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,id TEXT NOT NULL,expires_at INTEGER NOT NULL,PRIMARY KEY(upload_id,id));
CREATE TRIGGER read_lease_guard BEFORE INSERT ON read_leases WHEN (SELECT lease LIKE 'gc:%' FROM uploads WHERE id=NEW.upload_id) BEGIN SELECT RAISE(ABORT,'CONTENT_RETIRED');END;
CREATE INDEX uploads_cleanup ON uploads(state,expires_at);
CREATE INDEX uploads_account_state ON uploads(account_id,state);
CREATE TRIGGER pending_limit BEFORE INSERT ON uploads WHEN (SELECT COUNT(*) FROM uploads WHERE account_id=NEW.account_id AND state IN ('pending','verifying'))>=10 BEGIN SELECT RAISE(ABORT,'UPLOAD_LIMIT'); END;
CREATE TABLE parts(upload_id TEXT NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,idx INTEGER NOT NULL,key TEXT NOT NULL UNIQUE,size INTEGER NOT NULL,sha256 TEXT NOT NULL,ready INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(upload_id,idx));
CREATE TABLE items(account_id TEXT NOT NULL REFERENCES accounts(id),id TEXT NOT NULL,version INTEGER NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,active_upload TEXT,kind TEXT NOT NULL,name TEXT NOT NULL,mime_type TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT,updated_at INTEGER NOT NULL,PRIMARY KEY(account_id,id));
CREATE TABLE changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,account_id TEXT NOT NULL,item_id TEXT NOT NULL,record TEXT NOT NULL);
CREATE INDEX changes_account_seq ON changes(account_id,seq);
CREATE TRIGGER item_insert AFTER INSERT ON items BEGIN
 INSERT INTO changes(account_id,item_id,record) VALUES(NEW.account_id,NEW.id,json_object('id',NEW.id,'version',NEW.version,'deleted',json(CASE WHEN NEW.deleted=1 THEN 'true' ELSE 'false' END),'kind',NEW.kind,'name',NEW.name,'mime_type',NEW.mime_type,'size',NEW.size,'sha256',NEW.sha256,'updated_at',strftime('%Y-%m-%dT%H:%M:%fZ',NEW.updated_at/1000.0,'unixepoch')));
END;
CREATE TRIGGER item_update AFTER UPDATE ON items BEGIN
 INSERT INTO changes(account_id,item_id,record) VALUES(NEW.account_id,NEW.id,json_object('id',NEW.id,'version',NEW.version,'deleted',json(CASE WHEN NEW.deleted=1 THEN 'true' ELSE 'false' END),'kind',NEW.kind,'name',NEW.name,'mime_type',NEW.mime_type,'size',NEW.size,'sha256',NEW.sha256,'updated_at',strftime('%Y-%m-%dT%H:%M:%fZ',NEW.updated_at/1000.0,'unixepoch')));
 UPDATE uploads SET state='garbage',garbage_after=unixepoch()*1000+600000 WHERE id=OLD.active_upload AND OLD.active_upload IS NOT NEW.active_upload;
END;
CREATE TRIGGER commit_cas BEFORE UPDATE OF state ON uploads WHEN NEW.state='committed' BEGIN
 SELECT CASE WHEN COALESCE((SELECT version FROM items WHERE account_id=NEW.account_id AND id=NEW.item_id),0)<>NEW.base_version THEN RAISE(ABORT,'VERSION_CONFLICT') END;
END;
CREATE TRIGGER commit_item AFTER UPDATE OF state ON uploads WHEN NEW.state='committed' AND OLD.state<>'committed' BEGIN
 INSERT INTO items(account_id,id,version,active_upload,kind,name,mime_type,size,sha256,updated_at) VALUES(NEW.account_id,NEW.item_id,NEW.base_version+1,NEW.id,NEW.kind,NEW.name,NEW.mime_type,NEW.size,NEW.sha256,unixepoch()*1000)
 ON CONFLICT(account_id,id) DO UPDATE SET version=excluded.version,deleted=0,active_upload=excluded.active_upload,kind=excluded.kind,name=excluded.name,mime_type=excluded.mime_type,size=excluded.size,sha256=excluded.sha256,updated_at=excluded.updated_at;
END;
