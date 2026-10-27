-- Preserve legacy usernames, verifiers, account IDs, sessions and content.
-- Canonical keys are backfilled with the Worker's Unicode case-fold library;
-- SQLite lower() cannot perform NFKC/full Unicode case folding.
ALTER TABLE accounts ADD COLUMN username_key TEXT;
ALTER TABLE accounts ADD COLUMN login_alias TEXT;
CREATE INDEX accounts_username_key ON accounts(username_key);
CREATE UNIQUE INDEX accounts_login_alias ON accounts(login_alias);
CREATE TABLE account_name_migration(id INTEGER PRIMARY KEY CHECK(id=1),complete INTEGER NOT NULL DEFAULT 0);
INSERT INTO account_name_migration(id) VALUES(1);
-- Duplicate canonical keys are allowed for existing accounts only. The trigger
-- protects concurrent new registrations after backfill without merging users.
CREATE TRIGGER accounts_username_ready BEFORE INSERT ON accounts
WHEN NEW.username_key IS NULL OR EXISTS(SELECT 1 FROM accounts WHERE username_key IS NULL)
BEGIN SELECT RAISE(ABORT,'USERNAME_MIGRATION_REQUIRED'); END;
CREATE TRIGGER accounts_username_unique BEFORE INSERT ON accounts
WHEN EXISTS(SELECT 1 FROM accounts WHERE username_key=NEW.username_key)
BEGIN SELECT RAISE(ABORT,'USERNAME_TAKEN'); END;
