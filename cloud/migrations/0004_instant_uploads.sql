CREATE TABLE instant_receipts(
 upload_id TEXT PRIMARY KEY,
 account_id TEXT NOT NULL REFERENCES accounts(id),
 item_id TEXT NOT NULL,
 base_version INTEGER NOT NULL,
 kind TEXT NOT NULL,
 name TEXT NOT NULL,
 mime_type TEXT NOT NULL,
 size INTEGER NOT NULL,
 sha256 TEXT NOT NULL,
 source_upload TEXT NOT NULL,
 result_version INTEGER NOT NULL,
 expires_at INTEGER NOT NULL
);
CREATE INDEX instant_receipts_expiry ON instant_receipts(expires_at);
CREATE INDEX uploads_instant_match ON uploads(account_id,sha256,size,state);
DROP TRIGGER item_update;
CREATE TRIGGER item_update AFTER UPDATE ON items BEGIN
 INSERT INTO changes(account_id,item_id,record) VALUES(NEW.account_id,NEW.id,json_object('id',NEW.id,'version',NEW.version,'deleted',json(CASE WHEN NEW.deleted=1 THEN 'true' ELSE 'false' END),'kind',NEW.kind,'name',NEW.name,'mime_type',NEW.mime_type,'size',NEW.size,'sha256',NEW.sha256,'updated_at',strftime('%Y-%m-%dT%H:%M:%fZ',NEW.updated_at/1000.0,'unixepoch'),'expires_at',CASE WHEN NEW.expires_at IS NULL THEN NULL ELSE strftime('%Y-%m-%dT%H:%M:%fZ',NEW.expires_at/1000.0,'unixepoch') END));
 UPDATE uploads SET state='garbage',garbage_after=unixepoch()*1000
 WHERE id=OLD.active_upload AND OLD.active_upload IS NOT NEW.active_upload
 AND NOT EXISTS(SELECT 1 FROM items WHERE active_upload=OLD.active_upload AND deleted=0);
END;
