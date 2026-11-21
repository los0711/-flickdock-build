-- A verified raw clipboard file may be referenced by several shelf items.
-- No duplicate R2 object or allocation is created by adding a shelf reference.
CREATE TABLE clipboard_upload_refs(
 clipboard_id TEXT NOT NULL,
 account_id TEXT NOT NULL REFERENCES accounts(id),
 upload_id TEXT NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
 expires_at INTEGER NOT NULL,
 PRIMARY KEY(clipboard_id,upload_id)
);
CREATE INDEX clipboard_upload_refs_upload ON clipboard_upload_refs(upload_id,expires_at);
DROP TRIGGER item_update;
CREATE TRIGGER item_update AFTER UPDATE ON items BEGIN
 INSERT INTO changes(account_id,item_id,record) VALUES(NEW.account_id,NEW.id,json_object('id',NEW.id,'version',NEW.version,'deleted',json(CASE WHEN NEW.deleted=1 THEN 'true' ELSE 'false' END),'kind',NEW.kind,'name',NEW.name,'mime_type',NEW.mime_type,'size',NEW.size,'sha256',NEW.sha256,'updated_at',strftime('%Y-%m-%dT%H:%M:%fZ',NEW.updated_at/1000.0,'unixepoch'),'expires_at',CASE WHEN NEW.expires_at IS NULL THEN NULL ELSE strftime('%Y-%m-%dT%H:%M:%fZ',NEW.expires_at/1000.0,'unixepoch') END));
 UPDATE uploads SET state='garbage',garbage_after=unixepoch()*1000
 WHERE id=OLD.active_upload AND OLD.active_upload IS NOT NEW.active_upload
 AND NOT EXISTS(SELECT 1 FROM items WHERE active_upload=OLD.active_upload AND deleted=0)
 AND NOT EXISTS(SELECT 1 FROM clipboard_upload_refs WHERE upload_id=OLD.active_upload AND expires_at>unixepoch()*1000);
END;
