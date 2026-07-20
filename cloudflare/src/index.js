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
async function dispatchWorker(env, app) {
  const res = await fetch(`https://api.github.com/repos/${env.GH_REPO}/dispatches`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.GH_TOKEN}`,
      'Accept': 'application/vnd.github+json',
      'User-Agent': 'ahmad-auto-publisher',
      'content-type': 'application/json',
    },
    body: JSON.stringify({ event_type: 'publish_app', client_payload: { app_id: app.app_id, download_url: app.download_url } }),
  });
  return res.ok;
}

// ---------- المنطق الأساسي: الطابور ----------
async function enqueueApps(env, apps) {
  // apps: [{id, name, version, download_url, rank}] بترتيب الصفحة (rank=0 أعلى)
  let added = 0;
  const today = ksaDay();
  for (const a of apps) {
    const bl = await env.DB.prepare('SELECT 1 FROM blacklist WHERE app_id=?').bind(a.id).first();
    if (bl) continue;
    // نُشر اليوم؟ تجاهل (منع تكرار مرة/يوم)
    if (await getSetting(env, 'dedup_per_day', '1') === '1') {
      const pub = await env.DB.prepare('SELECT 1 FROM published WHERE app_id=? AND published_day=?')
        .bind(a.id, today).first();
      if (pub) continue;
    }
    // موجود بالطابور؟ حدّث الإصدار/الرابط (نبقي الأحدث) دون تغيير ترتيبه
    const ex = await env.DB.prepare('SELECT app_id FROM queue WHERE app_id=?').bind(a.id).first();
    if (ex) {
      await env.DB.prepare('UPDATE queue SET version=?, download_url=?, name=? WHERE app_id=?')
        .bind(a.version || '', a.download_url, a.name || '', a.id).run();
      continue;
    }
    await env.DB.prepare('INSERT INTO queue(app_id,name,version,download_url,rank,added_at,status) VALUES(?,?,?,?,?,?,?)')
      .bind(a.id, a.name || '', a.version || '', a.download_url, a.rank ?? 9999, nowSec(), 'pending').run();
    added++;
  }
  if (added) await logEvent(env, 'info', `أُضيف ${added} تطبيق للطابور`);
  return added;
}

async function publishedLastHour(env) {
  const r = await env.DB.prepare('SELECT COUNT(*) c FROM published WHERE published_at >= ?')
    .bind(nowSec() - 3600).first();
  return r ? r.c : 0;
}

// يُنادى من الكرون: أطلق تطبيقاً واحداً إن سمحت القواعد
async function tick(env) {
  if (await getSetting(env, 'enabled', '1') !== '1') return 'disabled';
  const pausedUntil = parseInt(await getSetting(env, 'paused_until', '0'), 10);
  if (pausedUntil && nowSec() < pausedUntil) return 'paused';

  const perHour = parseInt(await getSetting(env, 'per_hour', '10'), 10);
  if (await publishedLastHour(env) >= perHour) return 'hour_full';

  const today = ksaDay();
  // التالي بالترتيب: الأعلى بالصفحة (rank أصغر) ثم الأقدم إدخالاً، وغير منشور اليوم، وغير قيد المعالجة
  const next = await env.DB.prepare(
    `SELECT * FROM queue
       WHERE status='pending'
         AND app_id NOT IN (SELECT app_id FROM published WHERE published_day=?)
       ORDER BY rank ASC, added_at ASC LIMIT 1`).bind(today).first();
  if (!next) return 'empty';

  await env.DB.prepare("UPDATE queue SET status='processing' WHERE app_id=?").bind(next.app_id).run();
  const ok = await dispatchWorker(env, next);
  if (!ok) {
    await env.DB.prepare("UPDATE queue SET status='pending' WHERE app_id=?").bind(next.app_id).run();
    await logEvent(env, 'error', `فشل إطلاق العامل للتطبيق ${next.app_id}`);
    return 'dispatch_failed';
  }
  await logEvent(env, 'info', `أُطلق العامل: ${next.name} (${next.app_id})`);
  return 'dispatched:' + next.app_id;
}

// يُنادى من العامل بعد نجاح النشر
async function markPublished(env, app_id, name, version) {
  const today = ksaDay();
  await env.DB.prepare('INSERT OR IGNORE INTO published(app_id,name,version,published_day,published_at) VALUES(?,?,?,?,?)')
    .bind(app_id, name || '', version || '', today, nowSec()).run();
  await env.DB.prepare('DELETE FROM queue WHERE app_id=?').bind(app_id).run();
  await logEvent(env, 'ok', `نُشر: ${name || app_id}`);
}

// ---------- لوحة التحكم (أزرار البوت) ----------
async function panelMain(env) {
  const enabled = await getSetting(env, 'enabled', '1') === '1';
  const perHour = await getSetting(env, 'per_hour', '10');
  const qc = (await env.DB.prepare('SELECT COUNT(*) c FROM queue').first()).c;
  const todayCount = (await env.DB.prepare('SELECT COUNT(*) c FROM published WHERE published_day=?').bind(ksaDay()).first()).c;
  const text =
    `<b>🧠 لوحة تحكم النشر</b>\n\n` +
    `الحالة: ${enabled ? '🟢 يعمل' : '🔴 متوقف'}\n` +
    `السرعة: ${perHour}/ساعة\n` +
    `بالطابور: ${qc}\n` +
    `نُشر اليوم: ${todayCount}`;
  const kb = [
    [{ text: enabled ? '⏸️ إيقاف' : '▶️ تشغيل', callback_data: 'toggle' }],
    [{ text: '📋 الطابور', callback_data: 'queue' }, { text: '📊 التقرير', callback_data: 'report' }],
    [{ text: '⚙️ السرعة', callback_data: 'rate' }, { text: '🕐 إيقاف مؤقت', callback_data: 'pause' }],
    [{ text: '🚫 القائمة السوداء', callback_data: 'black' }, { text: '✍️ الفوتر', callback_data: 'footer' }],
    [{ text: '🔄 تحديث', callback_data: 'home' }],
  ];
  return { text, kb };
}

async function handleCallback(env, cq) {
  const data = cq.data;
  const chatId = cq.message.chat.id;
  const msgId = cq.message.message_id;
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
    const rows = (await env.DB.prepare('SELECT name,version,app_id FROM queue ORDER BY rank ASC, added_at ASC LIMIT 15').all()).results;
    const body = rows.length ? rows.map((r, i) => `${i + 1}. ${H(r.name || r.app_id)} ${H(r.version || '')}`).join('\n') : 'الطابور فاضي';
    return edit(`<b>📋 الطابور (أول 15)</b>\n\n${body}`, [
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

  if (data === 'rate') {
    const kb = [[3, 5, 10].map(n => ({ text: `${n}/ساعة`, callback_data: `rate_${n}` })),
                [15, 20].map(n => ({ text: `${n}/ساعة`, callback_data: `rate_${n}` })), ...back];
    return edit('<b>⚙️ اختر السرعة</b>', kb);
  }
  if (data.startsWith('rate_')) {
    await setSetting(env, 'per_hour', data.split('_')[1]);
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
  return tg(env, 'answerCallbackQuery', { callback_query_id: cq.id });
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

    // بوت تلقرام
    if (url.pathname === '/telegram' && request.method === 'POST') {
      const u = await request.json();
      const from = u.callback_query ? u.callback_query.from : (u.message ? u.message.from : null);
      if (!from || String(from.id) !== String(env.OWNER_ID)) return new Response('ok'); // للمالك فقط
      if (u.callback_query) await handleCallback(env, u.callback_query);
      else if (u.message) await handleMessage(env, u.message);
      return new Response('ok');
    }

    // إدخال تطبيقات من الماسح
    if (url.pathname === '/enqueue' && request.method === 'POST') {
      if (request.headers.get('x-secret') !== env.ENQUEUE_SECRET) return new Response('forbidden', { status: 403 });
      const { apps } = await request.json();
      const added = await enqueueApps(env, apps || []);
      return Response.json({ ok: true, added });
    }

    // تأكيد نشر من العامل
    if (url.pathname === '/published' && request.method === 'POST') {
      if (request.headers.get('x-secret') !== env.ENQUEUE_SECRET) return new Response('forbidden', { status: 403 });
      const { app_id, name, version } = await request.json();
      await markPublished(env, app_id, name, version);
      return Response.json({ ok: true });
    }

    // فشل من العامل (يرجّع للطابور + تنبيه المالك)
    if (url.pathname === '/failed' && request.method === 'POST') {
      if (request.headers.get('x-secret') !== env.ENQUEUE_SECRET) return new Response('forbidden', { status: 403 });
      const { app_id, error } = await request.json();
      await env.DB.prepare("UPDATE queue SET status='pending' WHERE app_id=?").bind(app_id).run();
      await logEvent(env, 'error', `فشل ${app_id}: ${error}`);
      await tg(env, 'sendMessage', { chat_id: env.OWNER_ID, text: `⚠️ فشل نشر التطبيق ${app_id}\n${error}` });
      return Response.json({ ok: true });
    }

    // ويبهوك أحمد (اختياري — لو فُعّل مستقبلاً)
    if (url.pathname === '/webhook/ahmad' && request.method === 'POST') {
      await logEvent(env, 'info', 'وصل ويبهوك من أحمد');
      return new Response('ok');
    }

    if (url.pathname === '/') return new Response('ahmad-auto-publisher: alive');
    return new Response('not found', { status: 404 });
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env));
  },
};
