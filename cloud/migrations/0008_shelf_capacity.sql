-- Logical shelf copies and in-flight reservations share one account budget.
-- Manual transfers and raw clipboard objects do not consume this shelf quota.
CREATE VIEW shelf_usage AS
 SELECT account_id,size FROM items
 WHERE deleted=0 AND kind<>'transfer' AND (expires_at IS NULL OR expires_at>unixepoch()*1000)
 UNION ALL
 SELECT account_id,size FROM uploads
 WHERE state IN ('pending','verifying') AND kind<>'transfer' AND expires_at>unixepoch()*1000;

CREATE TRIGGER shelf_reserve BEFORE INSERT ON uploads
 WHEN NEW.kind<>'transfer' AND NEW.state IN ('pending','verifying') BEGIN
 SELECT CASE WHEN NEW.size+COALESCE((SELECT SUM(size) FROM shelf_usage WHERE account_id=NEW.account_id),0)>1073741824
 THEN RAISE(ABORT,'SHELF_QUOTA') END;
END;
CREATE TRIGGER shelf_insert BEFORE INSERT ON items WHEN NEW.deleted=0 AND NEW.kind<>'transfer' BEGIN
 SELECT CASE WHEN NEW.size
  +COALESCE((SELECT SUM(size) FROM items WHERE account_id=NEW.account_id AND id<>NEW.id AND deleted=0 AND kind<>'transfer' AND (expires_at IS NULL OR expires_at>unixepoch()*1000)),0)
  +COALESCE((SELECT SUM(size) FROM uploads WHERE account_id=NEW.account_id AND id<>NEW.active_upload AND state IN ('pending','verifying') AND kind<>'transfer' AND expires_at>unixepoch()*1000),0)>1073741824
 THEN RAISE(ABORT,'SHELF_QUOTA') END;
END;
CREATE TRIGGER shelf_replace BEFORE UPDATE OF size,active_upload,deleted ON items
 WHEN NEW.deleted=0 AND NEW.kind<>'transfer' BEGIN
 SELECT CASE WHEN NEW.size
  +COALESCE((SELECT SUM(size) FROM items WHERE account_id=NEW.account_id AND id<>NEW.id AND deleted=0 AND kind<>'transfer' AND (expires_at IS NULL OR expires_at>unixepoch()*1000)),0)
  +COALESCE((SELECT SUM(size) FROM uploads WHERE account_id=NEW.account_id AND id<>NEW.active_upload AND state IN ('pending','verifying') AND kind<>'transfer' AND expires_at>unixepoch()*1000),0)>1073741824
 THEN RAISE(ABORT,'SHELF_QUOTA') END;
END;

DROP TRIGGER commit_item;
CREATE TRIGGER commit_item AFTER UPDATE OF state ON uploads WHEN NEW.state='committed' AND OLD.state<>'committed' BEGIN
 INSERT INTO items(account_id,id,version,active_upload,kind,name,mime_type,size,sha256,updated_at,expires_at)
 VALUES(NEW.account_id,NEW.item_id,NEW.base_version+1,NEW.id,NEW.kind,NEW.name,NEW.mime_type,NEW.size,NEW.sha256,unixepoch()*1000,
 CASE WHEN NEW.kind='transfer' THEN unixepoch()*1000+CASE WHEN NEW.size>=104857600 THEN 7200000 ELSE 86400000 END ELSE (CAST((unixepoch()+28800)/86400 AS INTEGER)+1)*86400000-28800000 END)
 ON CONFLICT(account_id,id) DO UPDATE SET version=excluded.version,deleted=0,active_upload=excluded.active_upload,kind=excluded.kind,name=excluded.name,mime_type=excluded.mime_type,size=excluded.size,sha256=excluded.sha256,updated_at=excluded.updated_at,expires_at=excluded.expires_at;
END;
UPDATE items SET expires_at=(CAST((unixepoch()+28800)/86400 AS INTEGER)+1)*86400000-28800000 WHERE deleted=0 AND kind<>'transfer';
UPDATE uploads SET expires_at=(CAST((unixepoch()+28800)/86400 AS INTEGER)+1)*86400000-28800000 WHERE kind<>'transfer' AND state IN ('pending','verifying');
CREATE TRIGGER shelf_commit_deadline BEFORE UPDATE OF state ON uploads WHEN NEW.state='committed' AND NEW.kind<>'transfer' AND NEW.expires_at<=unixepoch()*1000 BEGIN
 SELECT RAISE(ABORT,'UPLOAD_EXPIRED');
END;
