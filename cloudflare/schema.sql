-- طابور التطبيقات المنتظرة (بالترتيب: الأقدم إدخالاً = الأعلى بالقائمة يُنشر أول)
CREATE TABLE IF NOT EXISTS queue (
  app_id        TEXT PRIMARY KEY,
  name          TEXT,
  version       TEXT,
  download_url  TEXT,
  rank          INTEGER,          -- ترتيب الظهور بصفحة "تم تحديثها مؤخراً" (0 = الأعلى)
  added_at      INTEGER,          -- وقت الإدخال (unix)
  status        TEXT DEFAULT 'pending', -- pending | processing
  processing_at INTEGER DEFAULT 0       -- وقت بدء المعالجة (لاسترجاع العالق)
);
CREATE INDEX IF NOT EXISTS idx_queue_pick ON queue(status, rank, added_at);

-- سجل ما نُشر (لمنع التكرار: تطبيق واحد مرة باليوم)
CREATE TABLE IF NOT EXISTS published (
  app_id         TEXT,
  name           TEXT,
  version        TEXT,
  published_day  TEXT,            -- YYYY-MM-DD بتوقيت السعودية
  published_at   INTEGER,
  PRIMARY KEY (app_id, published_day)
);
CREATE INDEX IF NOT EXISTS idx_pub_day ON published(published_day);
CREATE INDEX IF NOT EXISTS idx_pub_at ON published(published_at);

-- الإعدادات (يتحكم فيها المالك عبر أزرار البوت)
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- القائمة السوداء (تطبيقات ما تُنشر أبداً)
CREATE TABLE IF NOT EXISTS blacklist (
  app_id TEXT PRIMARY KEY,
  name   TEXT
);

-- سجل الأحداث (لعرض التقارير بالبوت)
CREATE TABLE IF NOT EXISTS log (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  ts    INTEGER,
  kind  TEXT,      -- info | ok | error
  msg   TEXT
);

INSERT OR IGNORE INTO settings (key,value) VALUES
  ('enabled','1'),
  ('per_hour','10'),
  ('dedup_per_day','1'),
  ('footer',''),
  ('paused_until','0');
