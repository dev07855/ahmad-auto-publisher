-- مخطط قاعدة بيانات عقل النشر (D1) — مطابق للقاعدة الحيّة
-- ⚠️ الكود يعتمد على كل عمود هنا؛ أي نقص يكسر النظام (section/attempts/sections…).

-- طابور التطبيقات المنتظرة (الأقدم إدخالاً داخل القسم = يُنشر أول)
CREATE TABLE IF NOT EXISTS queue (
  app_id        TEXT PRIMARY KEY,
  name          TEXT,
  version       TEXT,
  download_url  TEXT,
  rank          INTEGER,                     -- ترتيب الظهور بصفحة القسم (0 = الأعلى)
  added_at      INTEGER,                     -- وقت الإدخال (unix)
  status        TEXT    DEFAULT 'pending',   -- pending | processing | failed
  processing_at INTEGER DEFAULT 0,           -- وقت بدء المعالجة (لاسترجاع العالق)
  section       TEXT    DEFAULT 'updates',   -- مفتاح القسم (يطابق sections.key)
  attempts      INTEGER DEFAULT 0            -- عدّاد محاولات الفشل (3 = failed)
);
CREATE INDEX IF NOT EXISTS idx_queue_pick ON queue(status, rank, added_at);

-- سجل ما نُشر (منع التكرار = مرة لكل app_id + version)
CREATE TABLE IF NOT EXISTS published (
  app_id        TEXT,
  version       TEXT,
  section       TEXT,                        -- القسم الذي نُشر منه (للعدّاد الساعي)
  published_day TEXT,                         -- YYYY-MM-DD بتوقيت السعودية
  published_at  INTEGER,
  PRIMARY KEY (app_id, version)
);
CREATE INDEX IF NOT EXISTS idx_pub_day ON published(published_day);
CREATE INDEX IF NOT EXISTS idx_pub_at  ON published(published_at);

-- الأقسام الديناميكية (يديرها المالك من البوت: إضافة/حذف/تفعيل/عدد)
CREATE TABLE IF NOT EXISTS sections (
  key     TEXT PRIMARY KEY,                   -- updates | games | design | modded | cat<رقم>
  name    TEXT,                               -- الاسم المعروض بالبوت والقناة
  path    TEXT,                               -- مسار صفحة أحمد (/last-app-update أو /category/N)
  quota   INTEGER DEFAULT 5,                  -- الحد الساعي للقسم (0 = موقّف)
  enabled INTEGER DEFAULT 1,                  -- 1 مفعّل | 0 موقّف
  ord     INTEGER DEFAULT 0                   -- ترتيب العرض
);

-- الإعدادات ومفاتيح الحالة (enabled, mix_mode, footer, tg_session, owners, subs_*, …)
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- القائمة السوداء (تطبيقات لا تُنشر أبداً)
CREATE TABLE IF NOT EXISTS blacklist (
  app_id TEXT PRIMARY KEY,
  name   TEXT
);

-- القنوات (يكتشفها البوت عند جعله مشرفاً؛ يفعّلها المالك ويختار أقسامها)
CREATE TABLE IF NOT EXISTS channels (
  chat_id  TEXT PRIMARY KEY,        -- معرّف القناة (-100…)
  name     TEXT,                    -- عنوان القناة
  username TEXT,                    -- @username إن وُجد (أضمن للنشر)
  enabled  INTEGER DEFAULT 0,       -- 0 = مكتشفة/موقوفة | 1 = مفعّلة
  added_at INTEGER
);

-- ربط القنوات بالأقسام (متعدّد لمتعدّد): قناة تنشر قسماً إن وُجد الصف
CREATE TABLE IF NOT EXISTS channel_sections (
  chat_id     TEXT,
  section_key TEXT,
  PRIMARY KEY (chat_id, section_key)
);

-- فهرس الدايلبات المخزّنة (الملفات نفسها في KV باسم الدايلب؛ الفعّال في settings.dylib_active)
CREATE TABLE IF NOT EXISTS dylibs (
  name     TEXT PRIMARY KEY,        -- اسم الملف (يُرسله المالك للبوت)
  size     INTEGER,
  added_at INTEGER
);

-- سجل الأحداث (لعرض التقارير بالبوت)
CREATE TABLE IF NOT EXISTS log (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  ts    INTEGER,
  kind  TEXT,                                 -- info | ok | error
  msg   TEXT
);

-- الإعدادات الافتراضية
INSERT OR IGNORE INTO settings (key,value) VALUES
  ('enabled','1'),
  ('mix_mode','0'),
  ('daily_summary','0'),
  ('footer',''),
  ('paused_until','0');

-- الأقسام الافتراضية (5 أقسام: تحديثات + ألعاب + تصاميم + معدلة + مشاهدة)
INSERT OR IGNORE INTO sections (key,name,path,quota,enabled,ord) VALUES
  ('updates','🔄 التحديثات','/last-app-update',5,1,0),
  ('games',  '🎮 الألعاب',  '/category/6',    5,1,1),
  ('design', '🎨 التصاميم', '/category/9',    5,1,2),
  ('modded', '🧰 المعدلة',  '/category/7',    5,1,3),
  ('cat11',  '📺 المشاهدة', '/category/11',   2,1,4);
