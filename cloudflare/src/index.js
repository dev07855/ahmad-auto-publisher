/**
 * عقل النشر الآلي — Cloudflare Worker
 * - يستقبل قائمة التطبيقات المحدّثة من الماسح (GitHub) ويعبّي الطابور
 * - كل بضع دقائق يطلق تطبيقاً واحداً (بحد 10/ساعة) لعامل GitHub لتحميله وحقنه ونشره
 * - لوحة تحكم كاملة عبر أزرار بوت تلقرام (على خاص المالك)
 * - يمنع تكرار نفس التطبيق أكثر من مرة باليوم
 *
 * أسرار (wrangler secret): TG_BOT_TOKEN, OWNER_ID, GH_TOKEN, GH_REPO (owner/name),
 *   ENQUEUE_SECRET, AHMAD_WEBHOOK_SECRET
 */

const KSA_OFFSET = 3 * 3600; // توقيت السعودية UTC+3

// ---------- أدوات ----------
const nowSec = () => Math.floor(Date.now() / 1000);
const ksaDay = (t = nowSec()) => new Date((t + KSA_OFFSET) * 1000).toISOString().slice(0, 10);
async function getSetting(env, k, d = null) {
  const r = await env.DB.prepare('SELECT value FROM settings WHERE key=?').bind(k).first();
  return r ? r.value : d;
}
async function setSetting(env, k, v) {
  await env.DB.prepare('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=?')
    .bind(k, String(v), String(v)).run();
}
async function logEvent(env, kind, msg) {
  await env.DB.prepare('INSERT INTO log(ts,kind,msg) VALUES(?,?,?)').bind(nowSec(), kind, msg).run();
}

// ترجمة أي خطأ تقني لسبب عربي واضح (يُستخدم بالتنبيه والسجل معاً — لا إنقلش للمالك أبداً)
function arErr(msg) {
  const m = String(msg || '');
  if (/OVERSIZE|file parts is invalid|entity too large|too big/i.test(m)) return 'التطبيق أكبر من حد تلقرام (٢ جيجا) — لا يمكن رفعه';
  if (/DEAD_APP|0-byte/i.test(m)) return 'ملف تالف على الخادم (فارغ)';
  if (/wait of \d+ seconds/i.test(m)) return 'تلقرام حدّ الرفع مؤقتاً (سيُعاد لاحقاً)';
  if (/two different IP|authorization key/i.test(m)) return 'الجلسة استُخدمت من مكانين معاً (سيُعاد لاحقاً)';
  if (/not an IPA/i.test(m)) return 'الملف المحمّل ليس تطبيقاً سليماً';
  if (/truncated/i.test(m)) return 'التحميل انقطع قبل اكتماله';
  if (/login failed/i.test(m)) return 'تعذّر تسجيل الدخول لموقع أحمد';
  if (/not found in recent/i.test(m)) return 'التطبيق ما عاد موجوداً بقائمة أحمد';
  if (/inject|lief|dylib/i.test(m)) return 'تعذّر حقن الإضافة بالتطبيق';
  if (/timed? ?out|timeout/i.test(m)) return 'انتهت المهلة (الملف كبير أو الشبكة بطيئة)';
  if (/connection|network|resolve|ECONN|SSL|certificate/i.test(m)) return 'انقطاع بالاتصال أثناء التحميل';
  if (/403|forbidden|401|unauthorized/i.test(m)) return 'رُفض الوصول (صلاحية أو جلسة منتهية)';
  if (/space|disk|memory/i.test(m)) return 'نفدت المساحة أثناء المعالجة';
  if (/chat not found|bot was blocked|CHANNEL_INVALID/i.test(m)) return 'مشكلة بالوصول للقناة (تحقق من صلاحية البوت)';
  return 'خطأ غير متوقع أثناء المعالجة';
}

// ---------- تلقرام ----------
async function tg(env, method, payload) {
  const r = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/${method}`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload),
  });
  return r.json();
}
const H = (s) => String(s ?? '').replace(/[<&>]/g, (c) => ({ '<': '&lt;', '&': '&amp;', '>': '&gt;' }[c]));

// الملّاك مخزّنون بجدول settings (تُعدَّل من البوت نفسه)؛ عند الفراغ نبدأ من سر OWNER_ID
async function getOwners(env) {
  const stored = await getSetting(env, 'owners', '');
  const ids = (stored || String(env.OWNER_ID || '')).split(',').map(s => s.trim()).filter(Boolean);
  return [...new Set(ids)];
}
async function setOwners(env, ids) {
  await setSetting(env, 'owners', [...new Set(ids.map(String))].filter(Boolean).join(','));
}
async function notifyOwners(env, text, extra = {}) {
  for (const id of await getOwners(env)) {
    await tg(env, 'sendMessage', { chat_id: id, parse_mode: 'HTML', text, ...extra });
  }
}

// ---------- تشغيل عامل GitHub ----------
async function dispatchWorker(env, app, footer, channels) {
  const res = await fetch(`https://api.github.com/repos/${env.GH_REPO}/dispatches`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.GH_TOKEN}`,
      'Accept': 'application/vnd.github+json',
      'User-Agent': 'ahmad-auto-publisher',
      'content-type': 'application/json',
    },
    body: JSON.stringify({ event_type: 'publish_app', client_payload: {
      app_id: app.app_id, download_url: app.download_url, footer: footer || '',
      channels: (channels || []).join(','),   // قنوات النشر لهذا التطبيق (فارغ = الرئيسية بالعامل)
    } }),
  });
  return res.ok;
}

// القنوات المفعّلة المشتركة بقسم معيّن. إن لم تُضبط أي قناة مفعّلة إطلاقاً → القناة الرئيسية (سلوك حالي محفوظ)
async function targetChannels(env, sectionKey) {
  const any = await env.DB.prepare('SELECT COUNT(*) c FROM channels WHERE enabled=1').first();
  if (!any || !any.c) {
    const main = env.TG_CHANNEL || await getSetting(env, 'channel', '');
    return main ? [main] : [];
  }
  const rows = (await env.DB.prepare(
    `SELECT c.chat_id, c.username FROM channels c JOIN channel_sections cs ON c.chat_id = cs.chat_id
     WHERE c.enabled = 1 AND cs.section_key = ?`).bind(sectionKey).all()).results || [];
  return rows.map(r => r.username || r.chat_id);   // @username أضمن للتحليل من الرقم الخام
}

// جلب بيانات قناة (للتحقق/التسمية)
async function getChat(env, chatId) {
  const r = await tg(env, 'getChat', { chat_id: chatId });
  return (r && r.ok) ? r.result : null;
}

// شاشة إدارة قناة: تفعيل + اختيار الأقسام (✅) + حذف
async function channelView(env, cid) {
  const c = await env.DB.prepare('SELECT * FROM channels WHERE chat_id=?').bind(cid).first();
  if (!c) return null;
  const secs = await loadSections(env, false);
  const subs = new Set(((await env.DB.prepare('SELECT section_key FROM channel_sections WHERE chat_id=?').bind(cid).all()).results || []).map(r => r.section_key));
  const kb = secs.map(s => [{ text: `${subs.has(s.key) ? '✅' : '⬜️'} ${s.name}`, callback_data: `chsec_${cid}_${s.key}` }]);
  kb.push([{ text: c.enabled ? '🔴 إيقاف القناة' : '🟢 تفعيل القناة', callback_data: `chtog_${cid}` }]);
  kb.push([{ text: '🗑️ حذف القناة', callback_data: `chdel_${cid}` }]);
  kb.push([{ text: '⬅️ القنوات', callback_data: 'channels' }]);
  const text = `<b>${H(c.name || cid)}</b>\nالحالة: ${c.enabled ? '🟢 مفعّلة' : '⚪️ موقوفة'}\n\nاختر الأقسام اللي تنشر بهالقناة (✅ = تنشر فيها):`;
  return { text, kb };
}

// شاشة الدايلبات: قائمة + المؤشّر ✅ للفعّال + زر حذف (نستخدم rowid بالأزرار لأمان الأسماء)
async function dylibsView(env) {
  const rows = (await env.DB.prepare('SELECT rowid AS id,name,size FROM dylibs ORDER BY added_at DESC').all()).results || [];
  const active = await getSetting(env, 'dylib_active', '');
  const kb = rows.map(r => [
    { text: `${r.name === active ? '✅' : '⬜️'} ${r.name}`, callback_data: `dyl_${r.id}` },
    { text: '🗑️', callback_data: `dyldel_${r.id}` },
  ]);
  const text = rows.length
    ? '<b>📎 الدايلب</b>\nالفعّال ✅ يُحقن بكل التطبيقات. اضغط اسماً ليصير الفعّال، أو 🗑️ للحذف.\n\n<i>لإضافة: أرسل ملف .dylib هنا.</i>'
    : '<b>📎 الدايلب</b>\n\nما فيه دايلبات بعد.\n\n<i>أرسل ملف .dylib للبوت هنا وبيتخزّن باسمه ويصير الفعّال.</i>';
  return { text, kb };
}

// اكتشاف تلقائي: عند جعل البوت مشرفاً بقناة → تُسجّل (موقوفة) ويُنبَّه المالك؛ وعند إزالته → تُوقَف
async function handleMyChatMember(env, upd) {
  const chat = upd.chat;
  if (!chat || chat.type !== 'channel') return;
  const chatId = String(chat.id);
  const status = upd.new_chat_member ? upd.new_chat_member.status : '';
  if (status === 'administrator') {
    const ex = await env.DB.prepare('SELECT 1 FROM channels WHERE chat_id=?').bind(chatId).first();
    if (!ex) {
      await env.DB.prepare('INSERT INTO channels(chat_id,name,username,enabled,added_at) VALUES(?,?,?,0,?)')
        .bind(chatId, chat.title || '', chat.username ? '@' + chat.username : '', nowSec()).run();
      await notifyOwners(env, `📢 <b>قناة جديدة اكتُشفت</b>\n\n${H(chat.title || chatId)}\n\nافتح «📢 القنوات» لتفعيلها واختيار أقسامها.`);
    }
  } else if (status === 'left' || status === 'kicked') {
    await env.DB.prepare("UPDATE channels SET enabled=0 WHERE chat_id=?").bind(chatId).run();
  }
}

// ---------- المنطق الأساسي: الطابور ----------
const isValidDL = (u) => typeof u === 'string' && u.startsWith('https://ahmad-up.com/download/link/');
const isValidId = (v) => /^\d+$/.test(String(v));

// فهرس أقسام موقع أحمد المتاحة للإضافة (name يظهر بالبوت والقناة)
const CATALOG = [
  { id: '6', name: '🎮 الألعاب' },
  { id: '7', name: '🧰 المعدلة' },
  { id: '8', name: '💰 المدفوعة' },
  { id: '9', name: '🎨 التصاميم' },
  { id: '10', name: '⚙️ الجلبريك' },
  { id: '11', name: '📺 المشاهدة' },
  { id: '13', name: '🕌 الإسلامية' },
  { id: '15', name: '🌍 Fake GPS' },
];

// الأقسام أصبحت ديناميكية (جدول sections) — تُدار بالكامل من البوت
async function loadSections(env, onlyEnabled = true) {
  const rows = (await env.DB.prepare('SELECT key,name,path,quota,enabled,ord FROM sections ORDER BY ord ASC, rowid ASC').all()).results || [];
  return onlyEnabled ? rows.filter(r => r.enabled) : rows;
}
async function sectionName(env, key) {
  const r = await env.DB.prepare('SELECT name FROM sections WHERE key=?').bind(key).first();
  return r ? r.name : key;
}
async function sectionExists(env, key) {
  return !!(await env.DB.prepare('SELECT 1 FROM sections WHERE key=?').bind(key).first());
}

async function enqueueApps(env, section, apps) {
  // apps: [{id, name, version, download_url, rank}] بترتيب صفحة القسم (rank=0 أعلى)
  if (!(await sectionExists(env, section))) section = 'updates';
  if (!Array.isArray(apps)) return 0;
  let added = 0;
  for (const a of apps) {
    if (!a || !isValidId(a.id) || !isValidDL(a.download_url)) continue;
    const ver = a.version || '';
    const bl = await env.DB.prepare('SELECT 1 FROM blacklist WHERE app_id=?').bind(a.id).first();
    if (bl) continue;
    // نُشر هذا الإصدار من قبل؟ تجاهل (منع تكرار بالإصدار — القسم يمشي للتالي)
    const pub = await env.DB.prepare('SELECT 1 FROM published WHERE app_id=? AND version=?')
      .bind(a.id, ver).first();
    if (pub) continue;
    // موجود بالطابور؟
    const ex = await env.DB.prepare('SELECT status, version FROM queue WHERE app_id=?').bind(a.id).first();
    if (ex) {
      if (ex.status === 'pending') {
        // حدّث بيانات pending فقط دون تغيير ترتيبه أو قسمه
        await env.DB.prepare('UPDATE queue SET version=?, download_url=?, name=? WHERE app_id=? AND status=?')
          .bind(ver, a.download_url, a.name || '', a.id, 'pending').run();
        continue;
      }
      // فشل سابقاً لكن نزل إصدار جديد → امنحه فرصة جديدة (احذف صف الفشل واتركه يُدرج من جديد)
      if (ex.status === 'failed' && ex.version !== ver) {
        await env.DB.prepare('DELETE FROM queue WHERE app_id=?').bind(a.id).run();
      } else {
        continue;  // قيد المعالجة، أو نفس النسخة الفاشلة → تجاهل
      }
    }
    await env.DB.prepare('INSERT INTO queue(app_id,name,version,download_url,rank,added_at,status,section) VALUES(?,?,?,?,?,?,?,?)')
      .bind(a.id, a.name || '', ver, a.download_url, a.rank ?? 9999, nowSec(), 'pending', section).run();
    added++;
  }
  if (added) await logEvent(env, 'info', `${await sectionName(env, section)}: أُضيف ${added}`);
  return added;
}

const PROCESSING_TIMEOUT = 20 * 60; // ثانية: بعدها نعتبر العامل مات ونعيد التطبيق للطابور

function safeCount(v, def) {
  const n = parseInt(v, 10);
  return (Number.isFinite(n) && n >= 0 && n <= 60) ? n : def; // fallback آمن، لا NaN أبداً
}

// كم نُشر/قيد المعالجة لقسم معيّن خلال آخر ساعة (كلاهما ضمن حدّ القسم)
async function sectionInFlight(env, section) {
  const p = await env.DB.prepare('SELECT COUNT(*) c FROM published WHERE section=? AND published_at >= ?')
    .bind(section, nowSec() - 3600).first();
  const q = await env.DB.prepare("SELECT COUNT(*) c FROM queue WHERE section=? AND status='processing'")
    .bind(section).first();
  return (p ? p.c : 0) + (q ? q.c : 0);
}

// أعِد أي تطبيق عالق في processing أقدم من المهلة إلى pending
async function reclaimStuck(env) {
  await env.DB.prepare("UPDATE queue SET status='pending' WHERE status='processing' AND processing_at < ?")
    .bind(nowSec() - PROCESSING_TIMEOUT).run();
  // نظّف صفوف الفشل القديمة (أسبوع+): تمنع تراكمها وتمنح التطبيق فرصة دورية لو رجع سليماً
  await env.DB.prepare("DELETE FROM queue WHERE status='failed' AND processing_at < ?")
    .bind(nowSec() - 7 * 86400).run();
}

// يُنادى من الكرون: أطلق تطبيقاً واحداً من قسم لم يبلغ حدّه بعد
async function tick(env) {
  if (await getSetting(env, 'enabled', '1') !== '1') return 'disabled';
  const pausedUntil = parseInt(await getSetting(env, 'paused_until', '0'), 10) || 0;
  if (pausedUntil && nowSec() < pausedUntil) return 'paused';

  await reclaimStuck(env);
  const footer = await getSetting(env, 'footer', '');
  const mix = (await getSetting(env, 'mix_mode', '0')) === '1';

  // رتّب الأقسام المؤهّلة (لها حصة متبقّية وتطبيق منتظر)
  const secs = await loadSections(env, true);
  const eligible = [];
  for (const s of secs) {
    const quota = safeCount(s.quota, 5);
    if (quota <= 0) continue;
    const infl = await sectionInFlight(env, s.key);
    if (infl >= quota) continue;
    const chans = await targetChannels(env, s.key);
    if (!chans.length) continue;                        // لا قناة مفعّلة تريد هذا القسم → تخطَّ
    eligible.push({ key: s.key, name: s.name, quota, infl, ratio: infl / quota, chans });
  }
  if (!eligible.length) return 'idle';
  // الخلط: اختر الأقل نسبةً (يوزّع بالتناوب)؛ التجميع: بترتيب الأقسام
  eligible.sort((a, b) => mix ? (a.ratio - b.ratio) : 0);

  for (const s of eligible) {
    const next = await env.DB.prepare(
      `SELECT * FROM queue WHERE section=? AND status='pending'
         ORDER BY rank ASC, added_at ASC LIMIT 1`).bind(s.key).first();
    if (!next) continue;

    // مطالبة ذرّية (تمنع السباق): لا يُطلق إلا من ينجح في pending→processing
    const claim = await env.DB.prepare(
      "UPDATE queue SET status='processing', processing_at=? WHERE app_id=? AND status='pending'")
      .bind(nowSec(), next.app_id).run();
    if (!claim.meta || claim.meta.changes !== 1) continue;

    const ok = await dispatchWorker(env, next, footer, s.chans);
    if (!ok) {
      await env.DB.prepare("UPDATE queue SET status='pending' WHERE app_id=?").bind(next.app_id).run();
      await logEvent(env, 'error', `فشل إطلاق العامل ${next.app_id}`);
      return 'dispatch_failed';
    }
    await logEvent(env, 'info', `${s.name}: ${next.name} (${next.app_id})`);
    return `dispatched:${s.key}:${next.app_id}`;
  }
  return 'idle';
}

// يُنادى من العامل بعد نجاح النشر
async function markPublished(env, app_id, name, version) {
  const today = ksaDay();
  // القسم من صف الطابور (قبل حذفه) — للعدّاد الساعي لكل قسم
  const row = await env.DB.prepare('SELECT section FROM queue WHERE app_id=?').bind(app_id).first();
  const section = row ? row.section : 'updates';
  await env.DB.prepare('INSERT OR IGNORE INTO published(app_id,version,section,published_day,published_at) VALUES(?,?,?,?,?)')
    .bind(app_id, version || '', section, today, nowSec()).run();
  await env.DB.prepare('DELETE FROM queue WHERE app_id=?').bind(app_id).run();
  await setSetting(env, 'last_publish_ts', nowSec());  // نبض النظام: آخر نشر ناجح
  await setSetting(env, 'health_alerted', '0');         // صفّر تنبيه التوقف (النظام حيّ)
  await logEvent(env, 'ok', `نُشر [${section}]: ${name || app_id}`);
}

// ---------- لوحة التحكم (أزرار البوت) ----------
function fmtDur(sec) {
  const m = Math.max(1, Math.round(sec / 60));
  return m >= 60 ? `${Math.round(m / 60)} ساعة` : `${m} دقيقة`;
}

async function panelMain(env) {
  const enabled = await getSetting(env, 'enabled', '1') === '1';
  const pausedUntil = parseInt(await getSetting(env, 'paused_until', '0'), 10) || 0;
  const paused = pausedUntil > nowSec();
  const mix = (await getSetting(env, 'mix_mode', '0')) === '1';
  const daily = (await getSetting(env, 'daily_summary', '0')) === '1';
  const secs = await loadSections(env, false);
  let total = 0;
  const lines = [];
  for (const s of secs) {
    const q = (await env.DB.prepare("SELECT COUNT(*) c FROM queue WHERE section=? AND status='pending'").bind(s.key).first()).c;
    const on = s.enabled ? '' : ' ⛔️';
    if (s.enabled) total += safeCount(s.quota, 5);
    lines.push(`${s.name}: ${s.quota}/ساعة  (بالطابور ${q})${on}`);
  }
  const todayCount = (await env.DB.prepare('SELECT COUNT(*) c FROM published WHERE published_day=?').bind(ksaDay()).first()).c;
  // الحالة الواضحة: متوقف / موقوف مؤقتاً (مع الوقت المتبقي) / يعمل
  const statusLine = !enabled ? '🔴 متوقف'
    : paused ? `⏸️ موقوف مؤقتاً (باقي ${fmtDur(pausedUntil - nowSec())})`
    : '🟢 يعمل';
  const text =
    `<b>🧠 لوحة تحكم النشر</b>\n\n` +
    `الحالة: ${statusLine}\n` +
    `النمط: ${mix ? '🔀 مخلوط' : '🗂️ مجمّع'}\n` +
    `الإجمالي: ${total}/ساعة\n\n` +
    lines.join('\n') +
    `\n\nنُشر اليوم: ${todayCount}`;
  const kb = [
    [{ text: enabled ? '⏸️ إيقاف' : '▶️ تشغيل', callback_data: 'toggle' }],
    [{ text: '🚀 نشر تطبيق فوراً', callback_data: 'pubnow' }],
    [{ text: '🔢 الأقسام والأعداد', callback_data: 'secs' }, { text: '📋 الطابور', callback_data: 'queue' }],
    [{ text: mix ? '🗂️ اجعله مجمّع' : '🔀 اجعله مخلوط', callback_data: 'mix' },
     { text: daily ? '🔕 إيقاف الملخص اليومي' : '🔔 تفعيل الملخص اليومي', callback_data: 'daily' }],
    [{ text: '👥 المشتركون', callback_data: 'subs' }, { text: '📊 التقرير', callback_data: 'report' }],
    [{ text: '🕐 إيقاف مؤقت', callback_data: 'pause' }],
    [{ text: '🚫 القائمة السوداء', callback_data: 'black' }, { text: '✍️ الفوتر', callback_data: 'footer' }],
    [{ text: '📢 القنوات', callback_data: 'channels' }, { text: '📎 الدايلب', callback_data: 'dylibs' }],
    [{ text: '👤 الملّاك', callback_data: 'owners' }, { text: '📖 دليل الاستخدام', callback_data: 'guide' }],
    [{ text: '🔄 تحديث', callback_data: 'home' }],
  ];
  return { text, kb };
}

async function handleCallback(env, cq) {
  const data = cq.data;
  const chatId = cq.message.chat.id;
  const msgId = cq.message.message_id;
  await tg(env, 'answerCallbackQuery', { callback_query_id: cq.id }); // يوقف مؤشّر التحميل على الزر
  const edit = (text, kb) => tg(env, 'editMessageText', {
    chat_id: chatId, message_id: msgId, text, parse_mode: 'HTML',
    reply_markup: { inline_keyboard: kb }, disable_web_page_preview: true,
  });
  const back = [[{ text: '⬅️ رجوع', callback_data: 'home' }]];

  if (data === 'home') { const p = await panelMain(env); return edit(p.text, p.kb); }

  // نشر فوري: اعرض تطبيقات الطابور بالاسم (أزرار) — اضغط واحداً ليُنشر الآن متخطياً الدور
  if (data === 'pubnow') {
    const rows = (await env.DB.prepare("SELECT app_id,name,version FROM queue WHERE status='pending' ORDER BY added_at DESC LIMIT 15").all()).results;
    if (!rows.length) return edit('<b>🚀 نشر فوري</b>\n\nالطابور فاضي حالياً — انتظر المسح القادم ثم جرّب.', back);
    const kb = rows.map(r => [{ text: `${r.name || 'تطبيق'}${r.version ? ' ' + r.version : ''}`.slice(0, 60), callback_data: `pub_${r.app_id}` }]);
    kb.push(...back);
    return edit('<b>🚀 نشر فوري</b>\nاضغط التطبيق اللي تبي تنشره الحين (يتخطّى الدور):', kb);
  }
  const pn = data.match(/^pub_(\d+)$/);
  if (pn) {
    const id = pn[1];
    const app = await env.DB.prepare("SELECT * FROM queue WHERE app_id=? AND status='pending'").bind(id).first();
    if (!app) return edit('⚠️ هذا التطبيق ما عاد بالطابور (نُشر أو أُزيل).', back);
    // مطالبة ذرّية ثم إطلاق فوري بغضّ النظر عن حدّ القسم
    const chans = await targetChannels(env, app.section);
    if (!chans.length) return edit('⚠️ ما فيه قناة مفعّلة تستقبل قسم هذا التطبيق.\nفعّل قناة واربطها بالقسم من «📢 القنوات».', back);
    const claim = await env.DB.prepare("UPDATE queue SET status='processing', processing_at=? WHERE app_id=? AND status='pending'").bind(nowSec(), id).run();
    if (!claim.meta || claim.meta.changes !== 1) return edit('⚠️ يُعالَج بالفعل الآن.', back);
    const ok = await dispatchWorker(env, app, await getSetting(env, 'footer', ''), chans);
    if (!ok) {
      await env.DB.prepare("UPDATE queue SET status='pending' WHERE app_id=?").bind(id).run();
      return edit('❌ تعذّر الإطلاق، جرّب بعد لحظات.', back);
    }
    await logEvent(env, 'info', `نشر فوري: ${app.name || id}`);
    return edit(`🚀 <b>يُنشر الآن:</b> ${H(app.name || id)}\n\nبيوصل القناة خلال دقيقة ✅`, back);
  }

  // حظر فوري من زر تنبيه التخطّي (بالاسم)
  const bk = data.match(/^blk_(\d+)$/);
  if (bk) {
    const id = bk[1];
    const q = await env.DB.prepare('SELECT name FROM queue WHERE app_id=?').bind(id).first();
    const nm = q && q.name ? q.name : id;
    await env.DB.prepare('INSERT OR IGNORE INTO blacklist(app_id,name) VALUES(?,?)').bind(id, nm).run();
    await env.DB.prepare('DELETE FROM queue WHERE app_id=?').bind(id).run();
    return edit(`⛔ حُظر: ${H(nm)}\n\nما عاد ينشر إطلاقاً.`, back);
  }

  if (data === 'toggle') {
    const cur = await getSetting(env, 'enabled', '1');
    await setSetting(env, 'enabled', cur === '1' ? '0' : '1');
    const p = await panelMain(env); return edit(p.text, p.kb);
  }

  if (data === 'mix') {
    const cur = await getSetting(env, 'mix_mode', '0');
    await setSetting(env, 'mix_mode', cur === '1' ? '0' : '1');
    const p = await panelMain(env); return edit(p.text, p.kb);
  }
  if (data === 'daily') {
    const cur = await getSetting(env, 'daily_summary', '0');
    await setSetting(env, 'daily_summary', cur === '1' ? '0' : '1');
    const p = await panelMain(env); return edit(p.text, p.kb);
  }

  if (data === 'queue') {
    let body = '';
    for (const s of await loadSections(env, false)) {
      const rows = (await env.DB.prepare("SELECT name,version FROM queue WHERE section=? AND status='pending' ORDER BY rank ASC, added_at ASC LIMIT 4").bind(s.key).all()).results;
      body += `\n<b>${s.name}</b>\n` + (rows.length ? rows.map(r => `• ${H(r.name)} ${H(r.version || '')}`).join('\n') : '—') + '\n';
    }
    return edit(`<b>📋 الطابور (أوائل كل قسم)</b>\n${body}`, [
      [{ text: '🗑️ تفريغ الطابور', callback_data: 'queue_clear' }], ...back]);
  }
  if (data === 'queue_clear') {
    await env.DB.prepare("DELETE FROM queue WHERE status='pending'").run();
    return edit('✅ فُرّغ الطابور.', back);
  }

  if (data === 'subs') {
    const subs = await getSubscriberCount(env);
    if (subs == null) {
      return edit('<b>👥 المشتركون</b>\n\n⚠️ تعذّر جلب العدد.\nتأكد أن البوت مشرف داخل القناة.', back);
    }
    const hist = await recordSubsSnapshot(env, subs);   // سجّل اليوم أيضاً عند الضغط
    const last7 = hist.slice(-7);
    let body = `<b>👥 مشتركو القناة</b>\n\nالعدد الآن: <b>${subs}</b>`;
    if (last7.length >= 2) {
      const AR = ['الأحد', 'الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت'];
      const counts = last7.map(e => e.c);
      const mx = Math.max(...counts), mn = Math.min(...counts), span = Math.max(1, mx - mn);
      let chart = '', prevC = null;
      for (const e of last7) {
        const filled = 1 + Math.round(((e.c - mn) / span) * 6);   // 1..7 مربّعات
        const bar = '▓'.repeat(filled) + '░'.repeat(7 - filled);
        const dow = AR[new Date(e.d + 'T12:00:00Z').getUTCDay()];
        const delta = prevC == null ? '' : e.c > prevC ? ` +${e.c - prevC}` : e.c < prevC ? ` ${e.c - prevC}` : ' =';
        chart += `${dow} ${bar} ${e.c}${delta}\n`;
        prevC = e.c;
      }
      const weekGrow = counts[counts.length - 1] - counts[0];
      body += `\n\n<b>آخر ${last7.length} أيام:</b>\n<pre>${chart}</pre>`;
      body += `نمو الفترة: ${weekGrow >= 0 ? '+' : ''}${weekGrow} ${weekGrow > 0 ? '▲' : weekGrow < 0 ? '▼' : ''}`;
      // 🔮 توقّع الوصول للمعلم القادم (كل 500) بمعدّل النمو الحالي
      const avg = weekGrow / (last7.length - 1);
      if (avg > 0.5) {
        const next = (Math.floor(subs / 500) + 1) * 500;
        const need = Math.ceil((next - subs) / avg);
        body += `\n\n🔮 بهذا المعدل توصل ${next} خلال ~${need} يوم`;
      }
    } else {
      body += `\n\n📈 بدأ التتبّع من اليوم — بتشوف النمو والرسم والتوقّع بعد يوم أو يومين.`;
    }
    return edit(body, [[{ text: '🔄 تحديث', callback_data: 'subs' }], ...back]);
  }

  if (data === 'report') {
    const today = (await env.DB.prepare('SELECT COUNT(*) c FROM published WHERE published_day=?').bind(ksaDay()).first()).c;
    const errs = (await env.DB.prepare("SELECT msg FROM log WHERE kind='error' ORDER BY id DESC LIMIT 3").all()).results;
    // أسماء آخر ما نُشر تُقرأ من سجل الأحداث (kind='ok' = "نُشر [قسم]: الاسم")
    const last = (await env.DB.prepare("SELECT msg FROM log WHERE kind='ok' ORDER BY id DESC LIMIT 5").all()).results;
    const lastTxt = last.map(r => `• ${H(r.msg)}`).join('\n') || '—';
    const errTxt = errs.length ? '\n\n⚠️ آخر أخطاء:\n' + errs.map(e => '• ' + H(e.msg)).join('\n') : '';
    return edit(`<b>📊 التقرير</b>\n\nنُشر اليوم: ${today}\n\nآخر ما نُشر:\n${lastTxt}${errTxt}`, back);
  }

  // قائمة الأقسام (تعديل/تفعيل/حذف/إضافة)
  if (data === 'secs') {
    const secs = await loadSections(env, false);
    const kb = secs.map(s => [{ text: `${s.enabled ? '' : '⛔️ '}${s.name}: ${s.quota}/ساعة`, callback_data: `sec_${s.key}` }]);
    kb.push([{ text: '➕ أضف قسم', callback_data: 'addsec' }]);
    kb.push(...back);
    return edit('<b>🔢 الأقسام والأعداد</b>\nاختر قسماً لتعديله، أو أضف قسماً جديداً:', kb);
  }
  // إدارة قسم محدد
  const secm = data.match(/^sec_([a-z0-9]+)$/);
  if (secm && await sectionExists(env, secm[1])) {
    const key = secm[1];
    const s = await env.DB.prepare('SELECT * FROM sections WHERE key=?').bind(key).first();
    const opts = [0, 3, 5, 8, 10, 15, 20];
    const kb = [
      opts.slice(0, 4).map(n => ({ text: String(n), callback_data: `setsec_${key}_${n}` })),
      opts.slice(4).map(n => ({ text: String(n), callback_data: `setsec_${key}_${n}` })),
      [{ text: '✏️ رقم مخصّص', callback_data: `numsec_${key}` }],
      [{ text: s.enabled ? '⛔️ إيقاف القسم' : '✅ تفعيل القسم', callback_data: `toggsec_${key}` }],
    ];
    if (key !== 'updates') kb.push([{ text: '🗑️ حذف القسم', callback_data: `delsec_${key}` }]);
    kb.push([{ text: '⬅️ الأقسام', callback_data: 'secs' }]);
    return edit(`<b>${s.name}</b>\nالعدد الحالي: ${s.quota}/ساعة\nاختر رقماً، أو «✏️ رقم مخصّص» لأي رقم:`, kb);
  }
  // وضع انتظار: أرسل رقماً فيصير عدد القسم
  const nm = data.match(/^numsec_([a-z0-9]+)$/);
  if (nm && await sectionExists(env, nm[1])) {
    await setSetting(env, 'await', 'num:' + nm[1]);
    return edit(`✏️ أرسل الآن الرقم اللي تبيه لعدد <b>${await sectionName(env, nm[1])}</b> بالساعة:`, [[{ text: '⬅️ رجوع', callback_data: `sec_${nm[1]}` }]]);
  }
  // حفظ عدد قسم
  const ms = data.match(/^setsec_([a-z0-9]+)_(\d+)$/);
  if (ms && await sectionExists(env, ms[1])) {
    await env.DB.prepare('UPDATE sections SET quota=? WHERE key=?').bind(safeCount(ms[2], 5), ms[1]).run();
    const p = await panelMain(env); return edit(p.text, p.kb);
  }
  // تفعيل/إيقاف قسم
  const tg2 = data.match(/^toggsec_([a-z0-9]+)$/);
  if (tg2 && await sectionExists(env, tg2[1])) {
    await env.DB.prepare('UPDATE sections SET enabled=1-enabled WHERE key=?').bind(tg2[1]).run();
    const p = await panelMain(env); return edit(p.text, p.kb);
  }
  // حذف قسم (عدا التحديثات) + إزالة تطبيقاته المنتظرة
  const dl = data.match(/^delsec_([a-z0-9]+)$/);
  if (dl && dl[1] !== 'updates' && await sectionExists(env, dl[1])) {
    await env.DB.prepare('DELETE FROM sections WHERE key=?').bind(dl[1]).run();
    await env.DB.prepare("DELETE FROM queue WHERE section=? AND status='pending'").bind(dl[1]).run();
    const p = await panelMain(env); return edit('🗑️ حُذف القسم.', p.kb);
  }
  // إضافة قسم — أزرار جاهزة للأقسام المتاحة (اضغط بس، بلا كتابة)
  if (data === 'addsec') {
    const existingPaths = new Set((await loadSections(env, false)).map(s => s.path));
    const avail = CATALOG.filter(c => !existingPaths.has(`/category/${c.id}`));
    if (!avail.length) return edit('<b>➕ أضف قسم</b>\n\nكل الأقسام مُضافة بالفعل ✅', [[{ text: '⬅️ الأقسام', callback_data: 'secs' }]]);
    const kb = avail.map(c => [{ text: `${c.name}`, callback_data: `addcat_${c.id}` }]);
    kb.push([{ text: '⬅️ الأقسام', callback_data: 'secs' }]);
    return edit('<b>➕ أضف قسم</b>\nاضغط القسم اللي تبي تضيفه (٥/ساعة افتراضياً، غيّره بعدها):', kb);
  }
  // تنفيذ الإضافة بضغطة
  const ac = data.match(/^addcat_(\d+)$/);
  if (ac) {
    const c = CATALOG.find(x => x.id === ac[1]);
    if (c) {
      const ord = ((await env.DB.prepare('SELECT MAX(ord) mx FROM sections').first()).mx || 0) + 1;
      await env.DB.prepare('INSERT OR REPLACE INTO sections(key,name,path,quota,enabled,ord) VALUES(?,?,?,?,1,?)')
        .bind('cat' + c.id, c.name, `/category/${c.id}`, 5, ord).run();
    }
    const p = await panelMain(env); return edit('✅ أُضيف القسم (سيبدأ بالمسح التالي).', p.kb);
  }

  if (data === 'pause') {
    const pu = parseInt(await getSetting(env, 'paused_until', '0'), 10) || 0;
    const nowState = pu > nowSec() ? `⏸️ موقوف مؤقتاً حالياً — باقي ${fmtDur(pu - nowSec())}` : '🟢 النشر يعمل الآن (غير موقوف)';
    const kb = [[{ text: 'ساعة', callback_data: 'pause_1' }, { text: '3 ساعات', callback_data: 'pause_3' }, { text: 'يوم', callback_data: 'pause_24' }],
                [{ text: '▶️ إلغاء الإيقاف المؤقت', callback_data: 'pause_0' }], ...back];
    return edit(`<b>🕐 إيقاف مؤقت للنشر</b>\n\n${nowState}\n\nاختر مدة الإيقاف، أو ألغِه:`, kb);
  }
  if (data.startsWith('pause_')) {
    const h = parseInt(data.split('_')[1], 10);
    await setSetting(env, 'paused_until', h ? nowSec() + h * 3600 : 0);
    const p = await panelMain(env);
    return edit(h ? `⏸️ تم الإيقاف المؤقت ${fmtDur(h * 3600)}. (شوف الحالة فوق)` : '▶️ أُلغي الإيقاف — النشر يعمل الآن.', p.kb);
  }

  // 👤 إدارة الملّاك — عرض القائمة، كل مالك جنبه زر حذف (يُمنع حذف الأخير)
  if (data === 'owners') {
    const owners = await getOwners(env);
    const me = String(cq.from.id);
    const rows = owners.map(id => (owners.length > 1
      ? [{ text: `🗑️ ${id}${id === me ? ' (أنت)' : ''}`, callback_data: `delowner_${id}` }]
      : [{ text: `${id}${id === me ? ' (أنت)' : ''}`, callback_data: 'owners' }]));
    rows.push([{ text: '➕ أضف مالك', callback_data: 'addowner' }]);
    rows.push(...back);
    return edit('<b>👤 ملّاك البوت</b>\nكل رقم يتحكّم بالبوت كامل.\nاضغط 🗑️ لإزالة مالك (ما يمكن إزالة الأخير):', rows);
  }
  const dow = data.match(/^delowner_(\d+)$/);
  if (dow) {
    const id = dow[1];
    let owners = await getOwners(env);
    if (owners.length <= 1) return edit('⚠️ لا يمكن إزالة المالك الوحيد.', [[{ text: '⬅️ رجوع', callback_data: 'owners' }]]);
    owners = owners.filter(x => x !== id);
    await setOwners(env, owners);
    if (id === String(cq.from.id)) {
      // أزال نفسه — يطلع من البوت (بلا لوحة)
      return edit('✅ طلعت من البوت — ما عاد لك تحكّم.\n\nإذا حبيت ترجع، صاحب البوت يقدر يضيفك.', []);
    }
    const me = String(cq.from.id);
    const rows = owners.map(x => (owners.length > 1
      ? [{ text: `🗑️ ${x}${x === me ? ' (أنت)' : ''}`, callback_data: `delowner_${x}` }]
      : [{ text: `${x}${x === me ? ' (أنت)' : ''}`, callback_data: 'owners' }]));
    rows.push([{ text: '➕ أضف مالك', callback_data: 'addowner' }]);
    rows.push(...back);
    return edit(`✅ أُزيل المالك ${H(id)}.\n\n<b>👤 ملّاك البوت</b>`, rows);
  }
  if (data === 'addowner') {
    await setSetting(env, 'await', 'addowner');
    return edit('➕ أرسل الآن رقم تلقرام الرقمي للمالك الجديد.\n(يجيبه من بوت @userinfobot):', [[{ text: '⬅️ رجوع', callback_data: 'owners' }]]);
  }

  // ═══ 📢 القنوات ═══
  if (data === 'channels') {
    const chans = (await env.DB.prepare('SELECT chat_id,name,enabled FROM channels ORDER BY added_at ASC').all()).results || [];
    const kb = chans.map(c => [{ text: `${c.enabled ? '🟢' : '⚪️'} ${c.name || c.chat_id}`, callback_data: `ch_${c.chat_id}` }]);
    kb.push(...back);
    const note = chans.length ? 'اختر قناة لإدارتها (تفعيل + أقسامها):' : 'ما فيه قنوات بعد.';
    return edit(`<b>📢 القنوات</b>\n${note}\n\n<i>لإضافة قناة: خلِّ البوت مشرفاً فيها، وتظهر هنا تلقائياً.</i>`, kb);
  }
  const chsecm = data.match(/^chsec_(-?\d+)_([a-z0-9]+)$/);
  if (chsecm) {
    const cid = chsecm[1], sk = chsecm[2];
    const ex = await env.DB.prepare('SELECT 1 FROM channel_sections WHERE chat_id=? AND section_key=?').bind(cid, sk).first();
    if (ex) await env.DB.prepare('DELETE FROM channel_sections WHERE chat_id=? AND section_key=?').bind(cid, sk).run();
    else await env.DB.prepare('INSERT OR IGNORE INTO channel_sections(chat_id,section_key) VALUES(?,?)').bind(cid, sk).run();
    const v = await channelView(env, cid);
    return v ? edit(v.text, v.kb) : edit('⚠️ القناة ما عادت موجودة.', [[{ text: '⬅️ القنوات', callback_data: 'channels' }]]);
  }
  const chtogm = data.match(/^chtog_(-?\d+)$/);
  if (chtogm) {
    await env.DB.prepare('UPDATE channels SET enabled=1-enabled WHERE chat_id=?').bind(chtogm[1]).run();
    const v = await channelView(env, chtogm[1]);
    return v ? edit(v.text, v.kb) : edit('⚠️ القناة ما عادت موجودة.', [[{ text: '⬅️ القنوات', callback_data: 'channels' }]]);
  }
  const chdelm = data.match(/^chdel_(-?\d+)$/);
  if (chdelm) {
    await env.DB.prepare('DELETE FROM channels WHERE chat_id=?').bind(chdelm[1]).run();
    await env.DB.prepare('DELETE FROM channel_sections WHERE chat_id=?').bind(chdelm[1]).run();
    return edit('🗑️ حُذفت القناة.', [[{ text: '⬅️ القنوات', callback_data: 'channels' }]]);
  }
  const chm = data.match(/^ch_(-?\d+)$/);
  if (chm) {
    const v = await channelView(env, chm[1]);
    return v ? edit(v.text, v.kb) : edit('⚠️ القناة ما عادت موجودة.', [[{ text: '⬅️ القنوات', callback_data: 'channels' }]]);
  }

  // ═══ 📎 الدايلب ═══
  if (data === 'dylibs') {
    const v = await dylibsView(env);
    return edit(v.text, [...v.kb, ...back]);
  }
  const dyldelm = data.match(/^dyldel_(\d+)$/);
  if (dyldelm) {
    const r = await env.DB.prepare('SELECT name FROM dylibs WHERE rowid=?').bind(dyldelm[1]).first();
    if (r) {
      await env.DYLIBS.delete(r.name);
      await env.DB.prepare('DELETE FROM dylibs WHERE rowid=?').bind(dyldelm[1]).run();
      if (await getSetting(env, 'dylib_active', '') === r.name) await setSetting(env, 'dylib_active', '');
    }
    const v = await dylibsView(env);
    return edit(v.text, [...v.kb, ...back]);
  }
  const dylm = data.match(/^dyl_(\d+)$/);
  if (dylm) {
    const r = await env.DB.prepare('SELECT name FROM dylibs WHERE rowid=?').bind(dylm[1]).first();
    if (r) await setSetting(env, 'dylib_active', r.name);
    const v = await dylibsView(env);
    return edit(v.text, [...v.kb, ...back]);
  }

  if (data === 'guide') {
    const g =
`<b>📖 دليل استخدام البوت</b>

<b>▸ التحكم</b>
⏸️ إيقاف/تشغيل — يوقف أو يكمّل النشر (الطابور محفوظ)
🕐 إيقاف مؤقت — يوقف لمدة تختارها ثم يرجع وحده
🚀 نشر فوراً — اختر تطبيقاً بالاسم يُنشر الآن متخطياً الدور

<b>▸ الأقسام</b>
🔢 الأقسام والأعداد — كم تطبيق/ساعة لكل قسم (٠ = إيقاف القسم) + إضافة/حذف
📋 الطابور — المنتظرون بكل قسم + تفريغ
🔀 مخلوط / 🗂️ مجمّع — يوزّع النشر بالتناوب أو قسماً قسماً

<b>▸ المتابعة</b>
👥 المشتركون — العدد + رسم ٧ أيام + النمو + توقّع
📊 التقرير — نُشر اليوم + آخر ما نُشر + آخر الأخطاء (عربي)
🔔 الملخص اليومي — تقرير كل ليلة

<b>▸ القنوات والدايلب</b>
📢 القنوات — أضف البوت مشرفاً بأي قناة فتظهر تلقائياً؛ فعّلها واختر أقسامها (كل قناة محتواها)
📎 الدايلب — أرسل ملف .dylib للبوت هنا فيُخزَّن باسمه؛ اضغط أي واحد ليصير الفعّال المحقون

<b>▸ الإدارة</b>
🚫 القائمة السوداء — الممنوعون (يُحظر بزر «⛔ احظره» مع أي تنبيه)
✍️ الفوتر — النص الثابت أسفل كل منشور
👤 الملّاك — إضافة/إزالة من يتحكّم بالبوت

<b>▸ تنبيهات تلقائية توصلك</b>
🔴 لو النشر توقّف • 🎉 عند كل معلم مشتركين • 📉 عند هبوط • 🗓️ تقرير كل جمعة

<i>كل شي آلي — البوت يسحب ويحقن وينشر وحده ٢٤ ساعة، بدون جهازك.</i>`;
    return edit(g, back);
  }

  if (data === 'black') {
    const rows = (await env.DB.prepare('SELECT name,app_id FROM blacklist LIMIT 20').all()).results;
    const body = rows.length ? rows.map(r => `• ${H(r.name || r.app_id)}`).join('\n') : 'فاضية';
    return edit(`<b>🚫 القائمة السوداء</b>\n\n${body}\n\nللحظر: اضغط زر «⛔ احظره» اللي يجيك مع تنبيه أي تطبيق.`, back);
  }
  if (data === 'footer') {
    const f = await getSetting(env, 'footer', '');
    await setSetting(env, 'await', 'footer');  // الرسالة التالية = الفوتر الجديد
    return edit(`<b>✍️ فوتر المنشور</b>\n\nالحالي:\n${H(f) || '(فاضي)'}\n\n✏️ أرسل الآن النص الجديد للفوتر (أو «-» لمسحه).`, back);
  }
}

async function handleMessage(env, msg) {
  // ادعم الرسائل المُحوّلة/الصور (نصها في caption لا text)
  const text = (msg.text || msg.caption || '').trim();
  const reply = (t) => tg(env, 'sendMessage', { chat_id: msg.chat.id, text: t });
  if (text === '/start' || text === '/panel' || text === 'لوحة' || text === '🧠 لوحتي') {
    await setSetting(env, 'await', '');  // أي ضغطة على اللوحة تلغي وضع الانتظار
    const p = await panelMain(env);
    // زر ثابت «🧠 لوحتي» يظهر جنب مربع الكتابة — اضغطه أي وقت بدل ما تكتب /start
    await tg(env, 'sendMessage', {
      chat_id: msg.chat.id, text: 'اضغط «🧠 لوحتي» أي وقت لفتح اللوحة.',
      reply_markup: { keyboard: [[{ text: '🧠 لوحتي' }]], resize_keyboard: true, is_persistent: true },
    });
    return tg(env, 'sendMessage', { chat_id: msg.chat.id, text: p.text, parse_mode: 'HTML', reply_markup: { inline_keyboard: p.kb } });
  }
  // استقبال ملف دايلب: يُخزَّن باسمه بمخزن KV ويُسجَّل بالجدول
  if (msg.document) {
    const doc = msg.document;
    const fname = (doc.file_name || '').trim();
    if (!/\.dylib$/i.test(fname)) return reply('❌ أرسل ملفاً بامتداد .dylib');
    if (doc.file_size && doc.file_size > 20 * 1024 * 1024) return reply('❌ الملف كبير (أقصى 20 ميجا للبوت).');
    const gf = await tg(env, 'getFile', { file_id: doc.file_id });
    if (!gf.ok || !gf.result || !gf.result.file_path) return reply('❌ تعذّر جلب الملف من تلقرام.');
    const fresp = await fetch(`https://api.telegram.org/file/bot${env.TG_BOT_TOKEN}/${gf.result.file_path}`);
    if (!fresp.ok) return reply('❌ تعذّر تنزيل الملف.');
    const buf = await fresp.arrayBuffer();
    await env.DYLIBS.put(fname, buf);
    await env.DB.prepare('INSERT OR REPLACE INTO dylibs(name,size,added_at) VALUES(?,?,?)').bind(fname, buf.byteLength, nowSec()).run();
    const active = await getSetting(env, 'dylib_active', '');
    if (!active) await setSetting(env, 'dylib_active', fname);
    return reply(`✅ حُفظ الدايلب «${fname}» (${Math.round(buf.byteLength / 1024)} ك.ب).${active ? '\nفعّله من «📎 الدايلب».' : '\nصار هو الفعّال المحقون.'}`);
  }

  const awaiting = await getSetting(env, 'await', '');
  // وضع انتظار الفوتر: الرسالة التالية بعد ضغط «الفوتر» تصير الفوتر
  if (awaiting === 'footer') {
    await setSetting(env, 'await', '');
    await setSetting(env, 'footer', text === '-' ? '' : text);
    return reply(text === '-' ? '✅ مُسح الفوتر.' : '✅ حُدّث الفوتر.');
  }
  // وضع انتظار رقم لقسم: الرسالة التالية (رقم) تصير عدد القسم
  if (awaiting.startsWith('num:')) {
    const key = awaiting.slice(4);
    await setSetting(env, 'await', '');
    if (!/^\d+$/.test(text) || !(await sectionExists(env, key))) return reply('❌ أرسل رقماً صحيحاً.');
    await env.DB.prepare('UPDATE sections SET quota=? WHERE key=?').bind(safeCount(text, 5), key).run();
    return reply(`✅ عدد ${await sectionName(env, key)} = ${safeCount(text, 5)}/ساعة.`);
  }
  // إضافة مالك جديد: الرسالة التالية بعد «➕ أضف مالك» = رقمه
  if (awaiting === 'addowner') {
    await setSetting(env, 'await', '');
    if (!/^\d{5,}$/.test(text)) return reply('❌ أرسل رقماً صحيحاً (أرقام فقط، من @userinfobot).');
    const owners = await getOwners(env);
    if (owners.includes(text)) return reply('ℹ️ هذا الرقم مالك بالفعل.');
    owners.push(text);
    await setOwners(env, owners);
    return reply(`✅ أُضيف المالك ${text}. صار يقدر يفتح البوت ويتحكم.`);
  }
  if (text.startsWith('فوتر:')) {
    await setSetting(env, 'footer', text.slice(5).trim());
    return reply('✅ حُدّث الفوتر.');
  }
  if (text.startsWith('حظر ')) {
    const id = text.slice(4).trim();
    await env.DB.prepare('INSERT OR IGNORE INTO blacklist(app_id) VALUES(?)').bind(id).run();
    await env.DB.prepare('DELETE FROM queue WHERE app_id=?').bind(id).run();
    return tg(env, 'sendMessage', { chat_id: msg.chat.id, text: `🚫 حُظر التطبيق ${id}.` });
  }
  // إضافة قسم: «قسم <رقم الكاتيجري> <الاسم> [العدد]»
  let m = text.match(/^قسم\s+(\d+)\s+(.+?)(?:\s+(\d+))?$/);
  if (m) {
    const catId = m[1], name = m[2].trim(), quota = safeCount(m[3], 5);
    const key = 'cat' + catId;
    const ord = ((await env.DB.prepare('SELECT MAX(ord) mx FROM sections').first()).mx || 0) + 1;
    await env.DB.prepare('INSERT OR REPLACE INTO sections(key,name,path,quota,enabled,ord) VALUES(?,?,?,?,1,?)')
      .bind(key, `📦 ${name}`, `/category/${catId}`, quota, ord).run();
    return tg(env, 'sendMessage', { chat_id: msg.chat.id, text: `✅ أُضيف قسم «${name}» (${quota}/ساعة). سيبدأ بالمسح التالي.` });
  }
  // عدد مخصّص لقسم: «عدد <key> <رقم>»
  m = text.match(/^عدد\s+([a-z0-9]+)\s+(\d+)$/);
  if (m && await sectionExists(env, m[1])) {
    await env.DB.prepare('UPDATE sections SET quota=? WHERE key=?').bind(safeCount(m[2], 5), m[1]).run();
    return tg(env, 'sendMessage', { chat_id: msg.chat.id, text: `✅ عدد ${m[1]} = ${safeCount(m[2], 5)}/ساعة.` });
  }
}

// ملخص يومي للمالك (يُرسل مرة عند أول تِكّة بعد الساعة 21 بتوقيت السعودية)
async function maybeDailySummary(env) {
  if ((await getSetting(env, 'daily_summary', '0')) !== '1') return;
  const t = nowSec() + KSA_OFFSET;
  const hour = new Date(t * 1000).getUTCHours();
  if (hour < 21) return;                          // بعد 9 مساءً السعودية
  const today = ksaDay();
  if ((await getSetting(env, 'daily_last', '')) === today) return; // مرة واحدة اليوم
  await setSetting(env, 'daily_last', today);
  const secs = await loadSections(env, false);
  let lines = [];
  for (const s of secs) {
    const c = (await env.DB.prepare('SELECT COUNT(*) c FROM published WHERE section=? AND published_day=?').bind(s.key, today).first()).c;
    lines.push(`${s.name}: ${c}`);
  }
  const total = (await env.DB.prepare('SELECT COUNT(*) c FROM published WHERE published_day=?').bind(today).first()).c;
  const errs = (await env.DB.prepare("SELECT COUNT(*) c FROM log WHERE kind='error' AND ts >= ?").bind(nowSec() - 86400).first()).c;
  // عدّاد المشتركين مع نمو اليوم (مقارنة بأمس)
  let subsLine = '';
  const subs = await getSubscriberCount(env);
  if (subs != null) {
    const prev = parseInt(await getSetting(env, 'subs_last', '0'), 10) || 0;
    const diff = subs - prev;
    const arrow = !prev ? '' : diff > 0 ? ` (+${diff} ▲)` : diff < 0 ? ` (${diff} ▼)` : ' (=)';
    subsLine = `\n\n👥 مشتركوك: ${subs}${arrow}`;
    await setSetting(env, 'subs_last', subs);
  }
  await notifyOwners(env, `<b>📊 ملخص اليوم (${today})</b>\n\nنُشر إجمالاً: ${total}${subsLine}\n\n${lines.join('\n')}\n\n⚠️ أخطاء: ${errs}`);
}

// عدد مشتركي القناة (البوت لازم يكون مشرفاً فيها) — يرجّع null إذا تعذّر (يتخطّى بهدوء)
async function getSubscriberCount(env) {
  const channel = env.TG_CHANNEL || await getSetting(env, 'channel', '');
  if (!channel) return null;
  try {
    const r = await tg(env, 'getChatMemberCount', { chat_id: channel });
    return (r && r.ok && typeof r.result === 'number') ? r.result : null;
  } catch { return null; }
}

// سجّل عدد اليوم بالتاريخ (JSON بالإعدادات) — إدخال واحد/يوم، نحتفظ بآخر 30 يوماً
async function recordSubsSnapshot(env, count) {
  const today = ksaDay();
  let hist = [];
  try { hist = JSON.parse(await getSetting(env, 'subs_history', '[]')) || []; } catch { hist = []; }
  hist = hist.filter(e => e && e.d !== today);
  hist.push({ d: today, c: count });
  hist = hist.slice(-30);
  await setSetting(env, 'subs_history', JSON.stringify(hist));
  return hist;
}

// مراقبة يومية للمشتركين: تسجيل + تنبيه المعالم (كل 500) + تنبيه الهبوط (مرة/يوم)
async function maybeSubsWatch(env) {
  const today = ksaDay();
  if ((await getSetting(env, 'subs_watch_day', '')) === today) return;   // مرة واحدة باليوم
  const count = await getSubscriberCount(env);
  if (count == null) return;                                             // تعذّر — نعيد بكرة
  await setSetting(env, 'subs_watch_day', today);
  const hist = await recordSubsSnapshot(env, count);
  const prevEntry = hist.filter(e => e.d !== today).slice(-1)[0];
  const prev = prevEntry ? prevEntry.c : 0;
  // 📉 تنبيه هبوط (نقص 10+ مشترك بيوم)
  if (prev && (prev - count) >= 10) {
    await notifyOwners(env, `📉 <b>تنبيه هبوط</b>\n\nنقص ${prev - count} مشترك اليوم (من ${prev} إلى ${count}).\nراجع آخر منشوراتك — قد يكون فيها ما أزعج المتابعين.`);
  }
  // 🎉 تنبيه المعالم (كل 500)
  const step = 500;
  const lastM = parseInt(await getSetting(env, 'subs_milestone', '0'), 10) || 0;
  const crossed = Math.floor(count / step) * step;
  if (lastM === 0) {
    await setSetting(env, 'subs_milestone', crossed);                   // خط أساس (بلا احتفال رجعي)
  } else if (crossed > lastM) {
    await setSetting(env, 'subs_milestone', crossed);
    await notifyOwners(env, `🎉 <b>مبروك!</b>\n\nقناتك وصلت <b>${crossed}</b> مشترك 🚀\nاستمر — نموّك ممتاز!`);
  }
}

// تنبيه «النشر متوقف»: نظام يعمل + طابور فيه منتظرون + ما نُشر شي من 6 ساعات (مرة واحدة حتى يعود)
async function maybeHealthCheck(env) {
  if (await getSetting(env, 'enabled', '1') !== '1') return;                 // متوقف يدوياً = طبيعي
  const pausedUntil = parseInt(await getSetting(env, 'paused_until', '0'), 10) || 0;
  if (pausedUntil && nowSec() < pausedUntil) return;                        // موقوف مؤقتاً = طبيعي
  // احسب المنتظرين في الأقسام المفعّلة فقط (قسم موقّف به منتظرون = وضع مقصود، لا إنذار)
  const enabledKeys = (await loadSections(env, true)).map(s => s.key);
  if (!enabledKeys.length) return;
  const ph = enabledKeys.map(() => '?').join(',');
  const pending = (await env.DB.prepare(`SELECT COUNT(*) c FROM queue WHERE status='pending' AND section IN (${ph})`).bind(...enabledKeys).first()).c;
  if (!pending) return;                                                     // ما فيه شي ينتظر = طبيعي
  const last = parseInt(await getSetting(env, 'last_publish_ts', '0'), 10) || 0;
  if (!last) return;                                                        // لم ينشر بعد أصلاً = لا إنذار كاذب
  const since = nowSec() - last;
  if (since < 6 * 3600) return;                                             // نُشر مؤخراً = تمام
  if (await getSetting(env, 'health_alerted', '0') === '1') return;         // نبّهنا مسبقاً
  await setSetting(env, 'health_alerted', '1');
  await notifyOwners(env, `🔴 <b>تنبيه: النشر متوقف</b>\n\nصار ${fmtDur(since)} وما نُشر ولا تطبيق، والطابور فيه ${pending} منتظر.\n\nالأسباب المحتملة:\n• اشتراكك بموقع أحمد انتهى\n• مشكلة بجيت هَب أو تلقرام\n\nافتح «🧠 لوحتي» ← 📊 التقرير لتشوف آخر خطأ.`);
}

// تقرير أسبوعي (كل جمعة بعد 9 مساءً السعودية، مرة واحدة)
async function maybeWeeklySummary(env) {
  const d = new Date((nowSec() + KSA_OFFSET) * 1000);
  if (d.getUTCDay() !== 5 || d.getUTCHours() < 21) return;                  // الجمعة بعد 9م
  const today = ksaDay();
  if ((await getSetting(env, 'weekly_last', '')) === today) return;
  await setSetting(env, 'weekly_last', today);
  const weekAgo = ksaDay(nowSec() - 6 * 86400);
  const total = (await env.DB.prepare('SELECT COUNT(*) c FROM published WHERE published_day >= ?').bind(weekAgo).first()).c;
  const secs = await loadSections(env, false);
  const lines = [];
  let topName = '—', topC = -1;
  for (const s of secs) {
    const c = (await env.DB.prepare('SELECT COUNT(*) c FROM published WHERE section=? AND published_day >= ?').bind(s.key, weekAgo).first()).c;
    lines.push(`${s.name}: ${c}`);
    if (c > topC) { topC = c; topName = s.name; }
  }
  const errs = (await env.DB.prepare("SELECT COUNT(*) c FROM log WHERE kind='error' AND ts >= ?").bind(nowSec() - 7 * 86400).first()).c;
  let subsLine = '';
  const subs = await getSubscriberCount(env);
  if (subs != null) {
    const prev = parseInt(await getSetting(env, 'subs_week_ago', '0'), 10) || 0;
    const diff = subs - prev;
    const arrow = !prev ? '' : diff > 0 ? ` (+${diff} ▲ هالأسبوع)` : diff < 0 ? ` (${diff} ▼ هالأسبوع)` : ' (=)';
    subsLine = `\n\n👥 المشتركون: ${subs}${arrow}`;
    await setSetting(env, 'subs_week_ago', subs);
  }
  await notifyOwners(env, `<b>🗓️ تقرير الأسبوع</b>\n\nنُشر إجمالاً: ${total}\nأنشط قسم: ${topName}${subsLine}\n\n${lines.join('\n')}\n\n⚠️ أخطاء الأسبوع: ${errs}`);
}

// تهيئة تلقائية لمرة واحدة: تسجيل القناة الرئيسية (كل الأقسام) + ضبط الويبهوك لاستقبال my_chat_member
async function maybeBootstrap(env) {
  if ((await getSetting(env, 'bootstrapped', '')) === '1') return;
  const cnt = (await env.DB.prepare('SELECT COUNT(*) c FROM channels').first()).c;
  if (!cnt && env.TG_CHANNEL) {
    const ch = await getChat(env, env.TG_CHANNEL);
    if (!ch) return;                                   // فشل getChat — نعيد المحاولة التِّكّة الجاية
    const cid = String(ch.id);
    await env.DB.prepare('INSERT OR IGNORE INTO channels(chat_id,name,username,enabled,added_at) VALUES(?,?,?,1,?)')
      .bind(cid, ch.title || '', ch.username ? '@' + ch.username : '', nowSec()).run();
    for (const s of await loadSections(env, false)) {
      await env.DB.prepare('INSERT OR IGNORE INTO channel_sections(chat_id,section_key) VALUES(?,?)').bind(cid, s.key).run();
    }
  }
  const wh = await tg(env, 'setWebhook', {
    url: 'https://ahmad-auto-publisher.tamerapp-api.workers.dev/telegram',
    secret_token: env.TG_WEBHOOK_SECRET,
    allowed_updates: ['message', 'callback_query', 'my_chat_member'],
  });
  if (wh && wh.ok) await setSetting(env, 'bootstrapped', '1');
}

// ---------- المُوجّه ----------
export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    const readJson = async () => { try { return await request.json(); } catch { return null; } };

    // بوت تلقرام — لازم توكن تلقرام السري (يمنع انتحال المالك عبر رقمه العام)
    if (url.pathname === '/telegram' && request.method === 'POST') {
      if (env.TG_WEBHOOK_SECRET &&
          request.headers.get('X-Telegram-Bot-Api-Secret-Token') !== env.TG_WEBHOOK_SECRET) {
        return new Response('forbidden', { status: 403 });
      }
      const u = await readJson();
      if (!u) return new Response('ok');
      // اكتشاف القنوات: عند تغيّر عضوية البوت بقناة — فقط لو المُغيِّر مالك
      if (u.my_chat_member) {
        const changer = u.my_chat_member.from ? String(u.my_chat_member.from.id) : '';
        if ((await getOwners(env)).includes(changer)) await handleMyChatMember(env, u.my_chat_member);
        return new Response('ok');
      }
      const from = u.callback_query ? u.callback_query.from : (u.message ? u.message.from : null);
      const owners = await getOwners(env);
      if (!from || !owners.includes(String(from.id))) return new Response('ok'); // للملّاك فقط
      if (u.callback_query) await handleCallback(env, u.callback_query);
      else if (u.message) await handleMessage(env, u.message);
      return new Response('ok');
    }

    // قائمة الأقسام المفعّلة (يقرأها الماسح ليعرف ماذا يمسح)
    if (url.pathname === '/sections' && request.method === 'GET') {
      if (request.headers.get('x-secret') !== env.ENQUEUE_SECRET) return new Response('forbidden', { status: 403 });
      const secs = await loadSections(env, true);
      return Response.json({ sections: secs.map(s => ({ key: s.key, path: s.path })) });
    }

    // الدايلب الفعّال (يسحبه العامل وقت الحقن بدل السر الثابت)
    if (url.pathname === '/dylib' && request.method === 'GET') {
      if (request.headers.get('x-secret') !== env.ENQUEUE_SECRET) return new Response('forbidden', { status: 403 });
      const active = await getSetting(env, 'dylib_active', '');
      if (!active) return new Response('', { status: 404 });
      const data = await env.DYLIBS.get(active, 'arrayBuffer');
      if (!data) return new Response('', { status: 404 });
      return new Response(data, { headers: { 'content-type': 'application/octet-stream' } });
    }

    // تهيئة لمرة واحدة: تسجيل القناة الرئيسية (كل الأقسام) + ضبط الويبهوك ليستقبل my_chat_member
    if (url.pathname === '/admin/setup' && request.method === 'POST') {
      if (request.headers.get('x-secret') !== env.ENQUEUE_SECRET) return new Response('forbidden', { status: 403 });
      let seeded = null;
      const cnt = (await env.DB.prepare('SELECT COUNT(*) c FROM channels').first()).c;
      if (!cnt && env.TG_CHANNEL) {
        const ch = await getChat(env, env.TG_CHANNEL);
        if (ch) {
          const cid = String(ch.id);
          await env.DB.prepare('INSERT OR IGNORE INTO channels(chat_id,name,username,enabled,added_at) VALUES(?,?,?,1,?)')
            .bind(cid, ch.title || '', ch.username ? '@' + ch.username : '', nowSec()).run();
          for (const s of await loadSections(env, false)) {
            await env.DB.prepare('INSERT OR IGNORE INTO channel_sections(chat_id,section_key) VALUES(?,?)').bind(cid, s.key).run();
          }
          seeded = { chat_id: cid, name: ch.title };
        }
      }
      const origin = new URL(request.url).origin;
      const wh = await tg(env, 'setWebhook', {
        url: origin + '/telegram',
        secret_token: env.TG_WEBHOOK_SECRET,
        allowed_updates: ['message', 'callback_query', 'my_chat_member'],
      });
      return Response.json({ seeded, webhook_ok: !!(wh && wh.ok) });
    }

    // جلسة بوت تلقرام المحفوظة (يعيد العامل استخدامها بدل تسجيل دخول كل مرة → لا FloodWait)
    if (url.pathname === '/tgsession') {
      if (request.headers.get('x-secret') !== env.ENQUEUE_SECRET) return new Response('forbidden', { status: 403 });
      if (request.method === 'GET') {
        return Response.json({ session: await getSetting(env, 'tg_session', '') });
      }
      if (request.method === 'POST') {
        const b = await readJson();
        if (b && typeof b.session === 'string') await setSetting(env, 'tg_session', b.session);
        return Response.json({ ok: true });
      }
    }

    // إدخال تطبيقات من الماسح
    if (url.pathname === '/enqueue' && request.method === 'POST') {
      if (request.headers.get('x-secret') !== env.ENQUEUE_SECRET) return new Response('forbidden', { status: 403 });
      const body = await readJson();
      if (!body) return new Response('bad request', { status: 400 });
      const added = await enqueueApps(env, body.section || 'updates', body.apps || []);
      return Response.json({ ok: true, added });
    }

    // تأكيد نشر من العامل
    if (url.pathname === '/published' && request.method === 'POST') {
      if (request.headers.get('x-secret') !== env.ENQUEUE_SECRET) return new Response('forbidden', { status: 403 });
      const body = await readJson();
      if (!body || !isValidId(body.app_id)) return new Response('bad request', { status: 400 });
      await markPublished(env, body.app_id, body.name, body.version);
      return Response.json({ ok: true });
    }

    // فشل من العامل (يرجّع للطابور + تنبيه المالك)
    if (url.pathname === '/failed' && request.method === 'POST') {
      if (request.headers.get('x-secret') !== env.ENQUEUE_SECRET) return new Response('forbidden', { status: 403 });
      const body = await readJson();
      if (!body || !isValidId(body.app_id)) return new Response('bad request', { status: 400 });
      const errMsg = String(body.error || '');
      // تطبيق تالف (0 بايت) = تخطٍّ فوري بلا إعادة محاولة (لا يؤخّر الطابور)
      const isDead = errMsg.includes('DEAD_APP');
      // أكبر من حد تلقرام (2 جيجا) = تخطٍّ فوري (بلا 3 محاولات) لكن مع تنبيه المالك مرة
      const isOversize = errMsg.includes('OVERSIZE') || /file parts is invalid|entity too large|request entity too large|too big/i.test(errMsg);
      const row = await env.DB.prepare('SELECT attempts FROM queue WHERE app_id=?').bind(body.app_id).first();
      const attempts = (row ? (row.attempts || 0) : 0) + 1;
      const giveUp = isDead || isOversize || attempts >= 3;  // تالف/كبير = فوراً، وإلا بعد 3 محاولات
      await env.DB.prepare(`UPDATE queue SET status=?, attempts=? WHERE app_id=?`)
        .bind(giveUp ? 'failed' : 'pending', attempts, body.app_id).run();
      const reason = arErr(errMsg);   // سبب عربي موحّد (للسجل والتنبيه معاً)
      await logEvent(env, 'error', `${isDead ? '☠️ تالف' : isOversize ? '📦 كبير' : 'فشل'} ${body.app_id}${(isDead || isOversize) ? '' : ` (محاولة ${attempts})`}: ${reason}`);
      if ((attempts >= 3 || isOversize) && !isDead) {
        const q = await env.DB.prepare('SELECT name FROM queue WHERE app_id=?').bind(body.app_id).first();
        const nm = q && q.name ? q.name : body.app_id;
        await notifyOwners(env, `⚠️ <b>تُخطّي: ${H(nm)}</b>\nالسبب: ${H(reason)}`, {
          reply_markup: { inline_keyboard: [[{ text: `⛔ احظره نهائياً`, callback_data: `blk_${body.app_id}` }]] },
        });
      }
      return Response.json({ ok: true });
    }

    if (url.pathname === '/') return new Response('ahmad-auto-publisher: alive');
    return new Response('not found', { status: 404 });
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      await maybeBootstrap(env);
      await tick(env);
      await maybeHealthCheck(env);
      await maybeSubsWatch(env);
      await maybeDailySummary(env);
      await maybeWeeklySummary(env);
    })());
  },
};
