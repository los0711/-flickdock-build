ALTER TABLE items ADD COLUMN expires_at INTEGER;
CREATE INDEX items_expiry ON items(deleted,expires_at);
CREATE INDEX rates_expiry ON rates(expires_at);
CREATE INDEX sessions_expiry ON sessions(expires_at);
CREATE INDEX auth_challenges_expiry ON auth_challenges(expires_at);
CREATE INDEX email_challenges_expiry ON email_challenges(expires_at);
CREATE INDEX read_leases_expiry ON read_leases(expires_at);
DROP TRIGGER item_insert;
DROP TRIGGER item_update;
DROP TRIGGER commit_item;
CREATE TRIGGER item_insert AFTER INSERT ON items BEGIN
 INSERT INTO changes(account_id,item_id,record) VALUES(NEW.account_id,NEW.id,json_object('id',NEW.id,'version',NEW.version,'deleted',json(CASE WHEN NEW.deleted=1 THEN 'true' ELSE 'false' END),'kind',NEW.kind,'name',NEW.name,'mime_type',NEW.mime_type,'size',NEW.size,'sha256',NEW.sha256,'updated_at',strftime('%Y-%m-%dT%H:%M:%fZ',NEW.updated_at/1000.0,'unixepoch'),'expires_at',CASE WHEN NEW.expires_at IS NULL THEN NULL ELSE strftime('%Y-%m-%dT%H:%M:%fZ',NEW.expires_at/1000.0,'unixepoch') END));
END;
CREATE TRIGGER item_update AFTER UPDATE ON items BEGIN
 INSERT INTO changes(account_id,item_id,record) VALUES(NEW.account_id,NEW.id,json_object('id',NEW.id,'version',NEW.version,'deleted',json(CASE WHEN NEW.deleted=1 THEN 'true' ELSE 'false' END),'kind',NEW.kind,'name',NEW.name,'mime_type',NEW.mime_type,'size',NEW.size,'sha256',NEW.sha256,'updated_at',strftime('%Y-%m-%dT%H:%M:%fZ',NEW.updated_at/1000.0,'unixepoch'),'expires_at',CASE WHEN NEW.expires_at IS NULL THEN NULL ELSE strftime('%Y-%m-%dT%H:%M:%fZ',NEW.expires_at/1000.0,'unixepoch') END));
 UPDATE uploads SET state='garbage',garbage_after=unixepoch()*1000 WHERE id=OLD.active_upload AND OLD.active_upload IS NOT NEW.active_upload;
END;
CREATE TRIGGER commit_item AFTER UPDATE OF state ON uploads WHEN NEW.state='committed' AND OLD.state<>'committed' BEGIN
 INSERT INTO items(account_id,id,version,active_upload,kind,name,mime_type,size,sha256,updated_at,expires_at) VALUES(NEW.account_id,NEW.item_id,NEW.base_version+1,NEW.id,NEW.kind,NEW.name,NEW.mime_type,NEW.size,NEW.sha256,unixepoch()*1000,CASE WHEN NEW.size>524288000 THEN unixepoch()*1000+86400000 ELSE NULL END)
 ON CONFLICT(account_id,id) DO UPDATE SET version=excluded.version,deleted=0,active_upload=excluded.active_upload,kind=excluded.kind,name=excluded.name,mime_type=excluded.mime_type,size=excluded.size,sha256=excluded.sha256,updated_at=excluded.updated_at,expires_at=excluded.expires_at;
END;
UPDATE items SET expires_at=updated_at+86400000 WHERE deleted=0 AND size>524288000;
