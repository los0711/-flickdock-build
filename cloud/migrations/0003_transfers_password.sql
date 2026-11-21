-- Pending metadata is account-private; original bytes remain unavailable until
-- the existing full-size/full-SHA commit transaction succeeds.
ALTER TABLE uploads ADD COLUMN device_id TEXT REFERENCES devices(id);
CREATE TABLE password_challenges(
 id TEXT PRIMARY KEY,
 account_id TEXT NOT NULL REFERENCES accounts(id),
 device_id TEXT NOT NULL REFERENCES devices(id),
 previous_salt TEXT NOT NULL,
 previous_verifier TEXT NOT NULL,
 new_salt TEXT NOT NULL,
 expires_at INTEGER NOT NULL,
 consumed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX password_challenges_expiry ON password_challenges(expires_at);
-- A manual cancel is terminal for this upload UUID, including when DELETE
-- reaches the service before a delayed initialization request. The receipt
-- outlives reclaimed bytes; it contains no file contents or authentication data.
CREATE TABLE cancelled_uploads(
 account_id TEXT NOT NULL REFERENCES accounts(id),
 id TEXT NOT NULL,
 item_id TEXT,
 expires_at INTEGER NOT NULL,
 PRIMARY KEY(account_id,id)
);
CREATE INDEX cancelled_uploads_expiry ON cancelled_uploads(expires_at);
CREATE TRIGGER cancelled_upload_guard BEFORE INSERT ON uploads WHEN EXISTS(
 SELECT 1 FROM cancelled_uploads c WHERE c.account_id=NEW.account_id AND c.id=NEW.id AND c.expires_at>unixepoch()*1000
) BEGIN SELECT RAISE(ABORT,'UPLOAD_CANCELLED'); END;
