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

// ---------- تلقرام ----------
async function tg(env, method, payload) {
  const r = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/${method}`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload),
  });
  return r.json();
}
const H = (s) => String(s ?? '').replace(/[<&>]/g, (c) => ({ '<': '&lt;', '&': '&amp;', '>': '&gt;' }[c]));

// ---------- تشغيل عامل GitHub ----------
async function dispatchWorker(env, app, footer) {
  const res = await fetch(`https://api.github.com/repos/${env.GH_REPO}/dispatches`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.GH_TOKEN}`,
      'Accept': 'application/vnd.github+json',
      'User-Agent': 'ahmad-auto-publisher',
      'content-type': 'application/json',
    },
    body: JSON.stringify({ event_type: 'publish_app', client_payload: { app_id: app.app_id, download_url: app.download_url, footer: footer || '' } }),
  });
  return res.ok;
}

// ---------- المنطق الأساسي: الطابور ----------
const isValidDL = (u) => typeof u === 'string' && u.startsWith('https://ahmad-up.com/download/link/');
const isValidId = (v) => /^\d+$/.test(String(v));
const SECTIONS = ['updates', 'games', 'design', 'modded'];
const perKey = { updates: 'per_updates', games: 'per_games', design: 'per_design', modded: 'per_modded' };
const SECTION_AR = { updates: '🔄 التحديثات', games: '🎮 الألعاب', design: '🎨 التصاميم', modded: '🧰 المعدلة' };

async function enqueueApps(env, section, apps) {
  // apps: [{id, name, version, download_url, rank}] بترتيب صفحة القسم (rank=0 أعلى)
  if (!SECTIONS.includes(section)) section = 'updates';
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
    // موجود بالطابور؟ حدّث بيانات pending فقط دون تغيير ترتيبه أو قسمه
    const ex = await env.DB.prepare('SELECT status FROM queue WHERE app_id=?').bind(a.id).first();
    if (ex) {
      if (ex.status === 'pending') {
        await env.DB.prepare('UPDATE queue SET version=?, download_url=?, name=? WHERE app_id=? AND status=?')
          .bind(ver, a.download_url, a.name || '', a.id, 'pending').run();
      }
      continue;
    }
    await env.DB.prepare('INSERT INTO queue(app_id,name,version,download_url,rank,added_at,status,section) VALUES(?,?,?,?,?,?,?,?)')
      .bind(a.id, a.name || '', ver, a.download_url, a.rank ?? 9999, nowSec(), 'pending', section).run();
    added++;
  }
  if (added) await logEvent(env, 'info', `${SECTION_AR[section]}: أُضيف ${added}`);
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
}

// يُنادى من الكرون: أطلق تطبيقاً واحداً من قسم لم يبلغ حدّه بعد
async function tick(env) {
  if (await getSetting(env, 'enabled', '1') !== '1') return 'disabled';
  const pausedUntil = parseInt(await getSetting(env, 'paused_until', '0'), 10) || 0;
  if (pausedUntil && nowSec() < pausedUntil) return 'paused';

  await reclaimStuck(env);
  const footer = await getSetting(env, 'footer', '');

  // مرّ على الأقسام بالترتيب؛ أطلق من أول قسم لم يبلغ حدّه وله تطبيق منتظر
  for (const section of SECTIONS) {
    const quota = safeCount(await getSetting(env, perKey[section], '5'), 5);
    if (quota <= 0) continue;
    if (await sectionInFlight(env, section) >= quota) continue;

    const next = await env.DB.prepare(
      `SELECT * FROM queue WHERE section=? AND status='pending'
         ORDER BY rank ASC, added_at ASC LIMIT 1`).bind(section).first();
    if (!next) continue;

    // مطالبة ذرّية (تمنع السباق): لا يُطلق إلا من ينجح في pending→processing
    const claim = await env.DB.prepare(
      "UPDATE queue SET status='processing', processing_at=? WHERE app_id=? AND status='pending'")
      .bind(nowSec(), next.app_id).run();
    if (!claim.meta || claim.meta.changes !== 1) continue;

    const ok = await dispatchWorker(env, next, footer);
    if (!ok) {
      await env.DB.prepare("UPDATE queue SET status='pending' WHERE app_id=?").bind(next.app_id).run();
      await logEvent(env, 'error', `فشل إطلاق العامل ${next.app_id}`);
      return 'dispatch_failed';
    }
    await logEvent(env, 'info', `${SECTION_AR[section]}: ${next.name} (${next.app_id})`);
    return `dispatched:${section}:${next.app_id}`;
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
  await logEvent(env, 'ok', `نُشر [${section}]: ${name || app_id}`);
}

// ---------- لوحة التحكم (أزرار البوت) ----------
async function sectionCounts(env) {
  const out = {};
  for (const s of SECTIONS) {
    out[s] = {
      quota: safeCount(await getSetting(env, perKey[s], '5'), 5),
      queued: (await env.DB.prepare("SELECT COUNT(*) c FROM queue WHERE section=? AND status='pending'").bind(s).first()).c,
    };
  }
  return out;
}

async function panelMain(env) {
  const enabled = await getSetting(env, 'enabled', '1') === '1';
  const sc = await sectionCounts(env);
  const total = SECTIONS.reduce((a, s) => a + sc[s].quota, 0);
  const todayCount = (await env.DB.prepare('SELECT COUNT(*) c FROM published WHERE published_day=?').bind(ksaDay()).first()).c;
  const lines = SECTIONS.map(s => `${SECTION_AR[s]}: ${sc[s].quota}/ساعة  (بالطابور ${sc[s].queued})`);
  const text =
    `<b>🧠 لوحة تحكم النشر</b>\n\n` +
    `الحالة: ${enabled ? '🟢 يعمل' : '🔴 متوقف'}\n` +
    `الإجمالي: ${total}/ساعة\n\n` +
    lines.join('\n') +
    `\n\nنُشر اليوم: ${todayCount}`;
  const kb = [
    [{ text: enabled ? '⏸️ إيقاف' : '▶️ تشغيل', callback_data: 'toggle' }],
    [{ text: '🔢 عدد كل قسم', callback_data: 'secs' }, { text: '📋 الطابور', callback_data: 'queue' }],
    [{ text: '📊 التقرير', callback_data: 'report' }, { text: '🕐 إيقاف مؤقت', callback_data: 'pause' }],
    [{ text: '🚫 القائمة السوداء', callback_data: 'black' }, { text: '✍️ الفوتر', callback_data: 'footer' }],
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

  if (data === 'toggle') {
    const cur = await getSetting(env, 'enabled', '1');
    await setSetting(env, 'enabled', cur === '1' ? '0' : '1');
    const p = await panelMain(env); return edit(p.text, p.kb);
  }

  if (data === 'queue') {
    let body = '';
    for (const s of SECTIONS) {
      const rows = (await env.DB.prepare("SELECT name,version FROM queue WHERE section=? AND status='pending' ORDER BY rank ASC, added_at ASC LIMIT 4").bind(s).all()).results;
      body += `\n<b>${SECTION_AR[s]}</b>\n` + (rows.length ? rows.map(r => `• ${H(r.name)} ${H(r.version || '')}`).join('\n') : '—') + '\n';
    }
    return edit(`<b>📋 الطابور (أوائل كل قسم)</b>\n${body}`, [
      [{ text: '🗑️ تفريغ الطابور', callback_data: 'queue_clear' }], ...back]);
  }
  if (data === 'queue_clear') {
    await env.DB.prepare("DELETE FROM queue WHERE status='pending'").run();
    return edit('✅ فُرّغ الطابور.', back);
  }

  if (data === 'report') {
    const today = (await env.DB.prepare('SELECT COUNT(*) c FROM published WHERE published_day=?').bind(ksaDay()).first()).c;
    const errs = (await env.DB.prepare("SELECT msg FROM log WHERE kind='error' ORDER BY id DESC LIMIT 3").all()).results;
    const last = (await env.DB.prepare("SELECT name,published_at FROM published ORDER BY published_at DESC LIMIT 5").all()).results;
    const lastTxt = last.map(r => `• ${H(r.name)}`).join('\n') || '—';
    const errTxt = errs.length ? '\n\n⚠️ آخر أخطاء:\n' + errs.map(e => '• ' + H(e.msg)).join('\n') : '';
    return edit(`<b>📊 التقرير</b>\n\nنُشر اليوم: ${today}\n\nآخر ما نُشر:\n${lastTxt}${errTxt}`, back);
  }

  // اختيار القسم لتعديل عدده
  if (data === 'secs') {
    const sc = await sectionCounts(env);
    const kb = SECTIONS.map(s => [{ text: `${SECTION_AR[s]}: ${sc[s].quota}/ساعة`, callback_data: `sec_${s}` }]);
    kb.push(...back);
    return edit('<b>🔢 عدد كل قسم بالساعة</b>\nاختر قسماً لتغيير عدده:', kb);
  }
  // شبكة أرقام لقسم محدد
  if (/^sec_(updates|games|design|modded)$/.test(data)) {
    const s = data.slice(4);
    const opts = [0, 3, 5, 8, 10, 15];
    const kb = [opts.slice(0, 3).map(n => ({ text: String(n), callback_data: `setsec_${s}_${n}` })),
                opts.slice(3).map(n => ({ text: String(n), callback_data: `setsec_${s}_${n}` })),
                [{ text: '⬅️ الأقسام', callback_data: 'secs' }]];
    return edit(`<b>${SECTION_AR[s]}</b>\nاختر العدد بالساعة (0 = إيقاف هذا القسم):`, kb);
  }
  // حفظ عدد قسم
  const ms = data.match(/^setsec_(updates|games|design|modded)_(\d+)$/);
  if (ms) {
    await setSetting(env, perKey[ms[1]], String(safeCount(ms[2], 5)));
    const p = await panelMain(env); return edit(p.text, p.kb);
  }

  if (data === 'pause') {
    const kb = [[{ text: 'ساعة', callback_data: 'pause_1' }, { text: '3 ساعات', callback_data: 'pause_3' }, { text: 'يوم', callback_data: 'pause_24' }],
                [{ text: '▶️ إلغاء الإيقاف', callback_data: 'pause_0' }], ...back];
    return edit('<b>🕐 إيقاف مؤقت للنشر</b>', kb);
  }
  if (data.startsWith('pause_')) {
    const h = parseInt(data.split('_')[1], 10);
    await setSetting(env, 'paused_until', h ? nowSec() + h * 3600 : 0);
    const p = await panelMain(env); return edit(h ? `⏸️ توقف ${h} ساعة.` : '▶️ استُؤنف.', p.kb);
  }

  if (data === 'black') {
    const rows = (await env.DB.prepare('SELECT name,app_id FROM blacklist LIMIT 20').all()).results;
    const body = rows.length ? rows.map(r => `• ${H(r.name || r.app_id)}`).join('\n') : 'فاضية';
    return edit(`<b>🚫 القائمة السوداء</b>\n\n${body}\n\nلإضافة: أرسل «حظر &lt;رقم التطبيق&gt;»`, back);
  }
  if (data === 'footer') {
    const f = await getSetting(env, 'footer', '');
    return edit(`<b>✍️ فوتر المنشور</b>\n\nالحالي:\n${H(f) || '(فاضي)'}\n\nلتغييره: أرسل «فوتر: النص الجديد»`, back);
  }
}

async function handleMessage(env, msg) {
  const text = (msg.text || '').trim();
  if (text === '/start' || text === '/panel' || text === 'لوحة') {
    const p = await panelMain(env);
    return tg(env, 'sendMessage', { chat_id: msg.chat.id, text: p.text, parse_mode: 'HTML', reply_markup: { inline_keyboard: p.kb } });
  }
  if (text.startsWith('فوتر:')) {
    await setSetting(env, 'footer', text.slice(5).trim());
    return tg(env, 'sendMessage', { chat_id: msg.chat.id, text: '✅ حُدّث الفوتر.' });
  }
  if (text.startsWith('حظر ')) {
    const id = text.slice(4).trim();
    await env.DB.prepare('INSERT OR IGNORE INTO blacklist(app_id) VALUES(?)').bind(id).run();
    await env.DB.prepare('DELETE FROM queue WHERE app_id=?').bind(id).run();
    return tg(env, 'sendMessage', { chat_id: msg.chat.id, text: `🚫 حُظر التطبيق ${id}.` });
  }
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
      const from = u.callback_query ? u.callback_query.from : (u.message ? u.message.from : null);
      if (!from || String(from.id) !== String(env.OWNER_ID)) return new Response('ok'); // للمالك فقط
      if (u.callback_query) await handleCallback(env, u.callback_query);
      else if (u.message) await handleMessage(env, u.message);
      return new Response('ok');
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
      await env.DB.prepare("UPDATE queue SET status='pending' WHERE app_id=?").bind(body.app_id).run();
      await logEvent(env, 'error', `فشل ${body.app_id}: ${String(body.error || '').slice(0, 200)}`);
      await tg(env, 'sendMessage', { chat_id: env.OWNER_ID, text: `⚠️ فشل نشر التطبيق ${H(body.app_id)}\n${H(String(body.error || '').slice(0, 200))}` });
      return Response.json({ ok: true });
    }

    if (url.pathname === '/') return new Response('ahmad-auto-publisher: alive');
    return new Response('not found', { status: 404 });
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env));
  },
};
