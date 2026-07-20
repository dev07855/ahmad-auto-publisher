-- طابور التطبيقات المنتظرة (بالترتيب: الأقدم إدخالاً = الأعلى بالقائمة يُنشر أول)
CREATE TABLE IF NOT EXISTS queue (
  app_id        TEXT PRIMARY KEY,
  name          TEXT,
  version       TEXT,
  download_url  TEXT,
  rank          INTEGER,          -- ترتيب الظهور بصفحة "تم تحديثها مؤخراً" (0 = الأعلى)
  added_at      INTEGER,          -- وقت الإدخال (unix)
  status        TEXT DEFAULT 'pending'  -- pending | processing
);

-- سجل ما نُشر (لمنع التكرار: تطبيق واحد مرة باليوم)
CREATE TABLE IF NOT EXISTS published (
  app_id         TEXT,
  name           TEXT,
  version        TEXT,
  published_day  TEXT,            -- YYYY-MM-DD بتوقيت السعودية
  published_at   INTEGER,
  PRIMARY KEY (app_id, published_day)
);

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
  ('day_boundary','midnight'),
  ('footer',''),
  ('paused_until','0');
