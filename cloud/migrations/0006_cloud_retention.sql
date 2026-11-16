-- Only a completed upload starts the lease. Existing records retain their
-- previous lease; do not retrospectively retire a user's older cloud items.
DROP TRIGGER commit_item;
CREATE TRIGGER commit_item AFTER UPDATE OF state ON uploads WHEN NEW.state='committed' AND OLD.state<>'committed' BEGIN
 INSERT INTO items(account_id,id,version,active_upload,kind,name,mime_type,size,sha256,updated_at,expires_at) VALUES(NEW.account_id,NEW.item_id,NEW.base_version+1,NEW.id,NEW.kind,NEW.name,NEW.mime_type,NEW.size,NEW.sha256,unixepoch()*1000,unixepoch()*1000+CASE WHEN NEW.size>=104857600 THEN 7200000 ELSE 86400000 END)
 ON CONFLICT(account_id,id) DO UPDATE SET version=excluded.version,deleted=0,active_upload=excluded.active_upload,kind=excluded.kind,name=excluded.name,mime_type=excluded.mime_type,size=excluded.size,sha256=excluded.sha256,updated_at=excluded.updated_at,expires_at=excluded.expires_at;
END;
