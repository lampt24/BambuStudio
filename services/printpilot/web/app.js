/* PrintPilot dashboard
 * - Same-origin API only; all server text rendered with textContent.
 * - Auth: Tailscale identity (/api/whoami) or a manager token kept in sessionStorage.
 */
(() => {
  'use strict';

  const TOKEN_KEY = 'printpilot_token';
  const REFRESH_MS = 10000;
  const STALE_IMAGE_SEC = 360;

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  const store = {
    get(key) { try { return sessionStorage.getItem(key) || ''; } catch { return ''; } },
    set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* private mode */ } },
    del(key) { try { sessionStorage.removeItem(key); } catch { /* ignore */ } },
  };

  const state = {
    token: store.get(TOKEN_KEY),
    tsUser: '',
    data: { printers: [], snapshots: [], alerts: [], analyses: [], devices: [], config: {} },
    alertFilter: 'open',
    alertPrinter: '',            // '' = all, printer_id, or '_system'
    events: { items: [], next: null, pagedBeyondFirst: false, loading: false },
    detailPrinterId: null,
    detailSnapshotId: null,
    cmd: null,                   // { printerId, id, label } while a control command is in flight
    timer: null,
    syncing: false,
  };

  // ---------------------------------------------------------------------------
  // Formatting
  // ---------------------------------------------------------------------------
  const STATUS = {
    printing: 'Đang in',
    idle: 'Sẵn sàng',
    paused: 'Tạm dừng',
    error: 'Lỗi',
    unknown: 'Không rõ',
    offline: 'Mất kết nối',
  };
  const OBJECTIVE = { quality: 'Chất lượng', strength: 'Độ bền', speed: 'Tốc độ', economy: 'Tiết kiệm' };
  const TRIGGER = { manual: 'Bấm AI auto setting', send: 'Trước khi gửi in' };

  function toDate(value) {
    if (!value) return null;
    const d = new Date(value);
    return isNaN(d.getTime()) ? null : d;
  }
  function ageSec(value) {
    const d = toDate(value);
    return d ? Math.max(0, (Date.now() - d.getTime()) / 1000) : Infinity;
  }
  function relTime(value) {
    const s = ageSec(value);
    if (!isFinite(s)) return '--';
    if (s < 45) return 'vừa xong';
    if (s < 3600) return `${Math.round(s / 60)} phút trước`;
    if (s < 86400) return `${Math.round(s / 3600)} giờ trước`;
    return `${Math.round(s / 86400)} ngày trước`;
  }
  function clock(value, withDate = false) {
    const d = toDate(value);
    if (!d) return '--';
    const t = d.toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' });
    return withDate ? `${t} ${d.toLocaleDateString('vi-VN', { day: '2-digit', month: '2-digit' })}` : t;
  }
  function minutes(m) {
    if (typeof m !== 'number' || !isFinite(m)) return '';
    const v = Math.max(0, Math.round(m));
    if (v >= 60) return `còn ${Math.floor(v / 60)}h${String(v % 60).padStart(2, '0')}`;
    return `còn ${v} phút`;
  }
  function temp(v) { return typeof v === 'number' ? `${Math.round(v)}°` : '--'; }
  function printerName(p) { return (p && (p.name || p.printer_id)) || 'Máy in'; }
  function printerStatus(p) { return p.stale ? 'offline' : (STATUS[p.status] ? p.status : 'unknown'); }

  // ---------------------------------------------------------------------------
  // API
  // ---------------------------------------------------------------------------
  class AuthError extends Error {}

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('X-PrintPilot-Request', '1');
    if (state.token) headers.set('Authorization', `Bearer ${state.token}`);
    const res = await fetch(path.startsWith('/') ? path : `/${path}`, {
      ...options,
      headers,
      cache: 'no-store',
      credentials: 'same-origin',
    });
    if (res.status === 401 || res.status === 403) throw new AuthError('unauthorized');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res;
  }

  // Snapshot images are cached as blob URLs (auth header cannot ride on <img src>)
  const images = new Map(); // id -> Promise<string>
  function imageUrl(id) {
    if (!images.has(id)) {
      const p = api(`/api/images/${encodeURIComponent(id)}`)
        .then((r) => r.blob())
        .then((b) => URL.createObjectURL(b))
        .catch((err) => { images.delete(id); throw err; });
      images.set(id, p);
    }
    return images.get(id);
  }
  function pruneImages(keep) {
    for (const [id, p] of images) {
      if (!keep.has(id)) {
        images.delete(id);
        p.then((url) => URL.revokeObjectURL(url)).catch(() => {});
      }
    }
  }
  function setImage(img, id, onFail) {
    img.dataset.id = id;
    imageUrl(id).then((url) => {
      if (img.dataset.id === id) img.src = url;
    }).catch(() => { if (img.dataset.id === id && onFail) onFail(); });
  }

  // ---------------------------------------------------------------------------
  // Derived data
  // ---------------------------------------------------------------------------
  function snapshotsOf(printerId) {
    return state.data.snapshots.filter((s) => s.printer_id === printerId);
  }
  function vision(snapshot) {
    if (!snapshot) return { state: 'none', text: 'Chưa có ảnh camera.' };
    if (ageSec(snapshot.captured_at || snapshot.created_at) > STALE_IMAGE_SEC) {
      return { state: 'none', text: 'Ảnh đã cũ, chưa thể đánh giá tình trạng hiện tại.' };
    }
    const r = snapshot.vision_result;
    if (snapshot.vision_status === 'complete' && r && typeof r === 'object') {
      const issues = Array.isArray(r.issues) ? r.issues.filter((i) => typeof i === 'string') : [];
      if (r.ok === false || issues.length) {
        return { state: 'warn', text: issues.length ? issues.join('; ') : (r.summary || 'Có dấu hiệu bất thường') };
      }
      return { state: 'ok', text: r.summary || 'Bản in trông bình thường.' };
    }
    if (snapshot.vision_status === 'pending' || snapshot.vision_status === 'queued') {
      return { state: 'none', text: 'AI đang xem ảnh…' };
    }
    if (snapshot.vision_status === 'error') return { state: 'none', text: 'AI không đánh giá được ảnh này.' };
    return { state: 'none', text: 'Chưa được AI đánh giá.' };
  }

  const ICON = {
    ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
    none: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>',
  };
  function visionBox(v) {
    const box = el('div', 'vision');
    box.dataset.state = v.state;
    const icon = el('span', 'vision-icon');
    icon.innerHTML = ICON[v.state] || ICON.none; // static markup only
    box.appendChild(icon);
    box.appendChild(el('span', 'vision-text', v.text));
    return box;
  }

  function metric(label, value, target) {
    const m = el('div', 'metric');
    m.appendChild(el('span', 'metric-label', label));
    const v = el('span', 'metric-value num', value);
    if (target) v.appendChild(el('span', 'metric-target', ` /${target}`));
    m.appendChild(v);
    return m;
  }

  // ---------------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------------
  function renderHealth() {
    const cfg = state.data.config || {};
    const set = (id, s, title) => { const n = $(id); n.dataset.state = s; n.title = title; };
    set('health-ai', cfg.ai_configured ? 'ok' : 'error', cfg.ai_configured ? 'AI sẵn sàng' : 'Chưa cấu hình AI');
    const devices = state.data.devices || [];
    const live = devices.filter((d) => !d.stale);
    set('health-bridge', !devices.length ? 'off' : live.length === devices.length ? 'ok' : 'warn',
      devices.length ? devices.map((d) => `${d.device_id}: ${d.stale ? 'mất tín hiệu' : 'hoạt động'} (${relTime(d.last_seen)})`).join('\n') : 'Chưa có cầu nối');
    set('health-notify', cfg.notifications_configured ? 'ok' : 'off',
      cfg.notifications_configured ? 'Đang gửi cảnh báo' : 'Chưa bật gửi cảnh báo ra ngoài');
  }

  function renderPrinters() {
    const root = $('printers');
    const printers = state.data.printers || [];
    $('printers-empty').hidden = printers.length > 0;
    const active = printers.filter((p) => printerStatus(p) === 'printing').length;
    $('printers-meta').textContent = printers.length
      ? `${printers.length} máy · ${active ? `${active} đang in` : 'không có máy nào đang in'}`
      : '';

    const seen = new Set();
    printers.forEach((p) => {
      seen.add(p.printer_id);
      let card = [...root.children].find((c) => c.dataset.printer === p.printer_id);
      if (!card) {
        card = buildPrinterCard(p.printer_id);
        root.appendChild(card);
      }
      updatePrinterCard(card, p);
    });
    [...root.children].forEach((card) => {
      if (!seen.has(card.dataset.printer)) card.remove();
    });
  }

  function buildPrinterCard(printerId) {
    const card = el('button', 'printer');
    card.type = 'button';
    card.dataset.printer = printerId;

    const media = el('div', 'printer-media');
    const img = el('img');
    img.alt = '';
    const empty = el('div', 'media-empty', 'Chưa có ảnh');
    const tagL = el('span', 'media-tag media-tag-left');
    tagL.appendChild(el('span', 'dot'));
    tagL.appendChild(el('span', 'status-text'));
    const tagR = el('span', 'media-tag media-tag-right num');
    media.append(img, empty, tagL, tagR);

    const body = el('div', 'printer-body');
    const head = el('div', 'printer-head');
    const titles = el('span', 'printer-titles');
    const count = el('span', 'alert-count');
    count.hidden = true;
    titles.append(el('span', 'printer-name'), count);
    head.append(titles, el('span', 'printer-sub'));
    const job = el('div', 'printer-job');
    const prog = el('div', 'progress-row');
    const bar = el('div', 'progress');
    bar.appendChild(el('span'));
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', '100');
    prog.append(bar, el('span', 'progress-pct num'), el('span', 'progress-eta num'));
    const metrics = el('div', 'metrics');
    const visionSlot = el('div', 'vision-slot');
    body.append(head, job, prog, metrics, visionSlot);

    card.append(media, body);
    card.addEventListener('click', () => openDetail(printerId));
    return card;
  }

  function updatePrinterCard(card, p) {
    const t = p.telemetry || {};
    const status = printerStatus(p);
    const snaps = snapshotsOf(p.printer_id);
    const latest = snaps[0];
    const name = printerName(p);

    card.setAttribute('aria-label', `${name}: ${STATUS[status]}. Mở chi tiết`);
    card.dataset.status = status;

    const media = card.querySelector('.printer-media');
    const img = media.querySelector('img');
    const empty = media.querySelector('.media-empty');
    const tagR = media.querySelector('.media-tag-right');
    if (latest) {
      const capturedAt = latest.captured_at || latest.created_at;
      media.dataset.stale = String(ageSec(capturedAt) > STALE_IMAGE_SEC);
      img.alt = `Camera ${name} lúc ${clock(capturedAt)}`;
      empty.hidden = true;
      if (img.dataset.id !== latest.id) {
        setImage(img, latest.id, () => { empty.hidden = false; empty.textContent = 'Không tải được ảnh'; });
      }
      tagR.textContent = `${ageSec(capturedAt) > STALE_IMAGE_SEC ? 'Ảnh cuối' : 'Ảnh'} ${relTime(capturedAt)}`;
      tagR.hidden = false;
    } else {
      img.removeAttribute('src');
      delete img.dataset.id;
      empty.hidden = false;
      tagR.hidden = true;
    }
    media.querySelector('.media-tag-left').dataset.status = status;
    media.querySelector('.status-text').textContent = STATUS[status];

    card.querySelector('.printer-name').textContent = name;
    const openAlerts = openAlertsFor(p.printer_id).length;
    const countEl = card.querySelector('.alert-count');
    countEl.hidden = openAlerts === 0;
    countEl.textContent = `${openAlerts} cảnh báo`;
    card.querySelector('.printer-sub').textContent = [p.model, `cập nhật ${relTime(p.last_seen)}`].filter(Boolean).join(' · ');

    const job = card.querySelector('.printer-job');
    job.textContent = t.job_name || '';
    job.title = t.job_name || '';
    job.hidden = !t.job_name;

    const hasProgress = typeof t.progress === 'number';
    const row = card.querySelector('.progress-row');
    row.hidden = !hasProgress;
    if (hasProgress) {
      const pct = Math.min(100, Math.max(0, t.progress));
      const bar = row.querySelector('.progress');
      bar.dataset.state = status;
      bar.setAttribute('aria-valuenow', String(pct));
      bar.firstChild.style.width = `${pct}%`;
      row.querySelector('.progress-pct').textContent = `${pct}%`;
      row.querySelector('.progress-eta').textContent = status === 'printing' ? minutes(t.remaining_min) : (pct >= 100 ? 'xong' : '');
    }

    const metrics = card.querySelector('.metrics');
    metrics.textContent = '';
    metrics.append(
      metric('Đầu in', temp(t.nozzle_temp), t.nozzle_target ? temp(t.nozzle_target) : ''),
      metric('Bàn in', temp(t.bed_temp), t.bed_target ? temp(t.bed_target) : ''),
      metric('Lớp', t.current_layer !== undefined ? `${t.current_layer}/${t.total_layers ?? '?'}` : '--'),
      metric('Quạt', t.fan_speed !== undefined ? `${t.fan_speed}%` : '--'),
    );

    const slot = card.querySelector('.vision-slot');
    slot.textContent = '';
    const resting = (status === 'idle' || status === 'offline') && latest
      && ageSec(latest.captured_at || latest.created_at) > STALE_IMAGE_SEC;
    let v = vision(latest);
    if (p.last_error) v = { state: 'warn', text: p.last_error };
    else if (resting) v = { state: 'none', text: 'Máy không in nên camera tạm nghỉ. Ảnh sẽ tự cập nhật khi bắt đầu in.' };
    slot.appendChild(visionBox(v));
  }

  const SEVERITY = { critical: 'Nghiêm trọng', error: 'Lỗi', warning: 'Cảnh báo', info: 'Thông tin' };

  function openAlertsFor(printerId) {
    return (state.data.alerts || []).filter((a) => !a.acknowledged && a.printer_id === printerId);
  }
  function printerNames() {
    return new Map((state.data.printers || []).map((p) => [p.printer_id, printerName(p)]));
  }

  function alertItem(a, names, showPrinter = true) {
    const li = el('li', 'alert');
    li.dataset.severity = a.severity || 'warning';
    li.dataset.ack = String(Boolean(a.acknowledged));
    li.appendChild(el('span', 'alert-bar'));
    const main = el('div', 'alert-main');
    main.appendChild(el('p', 'alert-msg', a.message || 'Cảnh báo'));
    const source = showPrinter ? (names.get(a.printer_id) || a.printer_id || a.device_id || 'Hệ thống') : '';
    main.appendChild(el('p', 'alert-meta',
      [SEVERITY[a.severity] || a.severity, source, `${clock(a.created_at, true)} (${relTime(a.created_at)})`].filter(Boolean).join(' · ')));
    li.appendChild(main);
    if (a.acknowledged) {
      if (a.acked_by) li.appendChild(el('span', 'alert-acked', `Đã xử lý bởi ${a.acked_by}`));
    } else {
      const btn = el('button', 'btn btn-sm', 'Đã xử lý');
      btn.type = 'button';
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        try {
          await api(`/api/alerts/${encodeURIComponent(a.id)}/ack`, { method: 'POST' });
          a.acknowledged = 1;
          a.acked_by = state.tsUser || 'mã quản lý';
          renderAlerts();
          renderPrinters();
          if ($('detail').open) renderDetail();
          loadEvents(true);
        } catch (err) {
          btn.disabled = false;
          handleError(err);
        }
      });
      li.appendChild(btn);
    }
    return li;
  }

  function renderAlertChips(open) {
    const root = $('alert-printers');
    root.textContent = '';
    const printers = state.data.printers || [];
    const systemOpen = open.filter((a) => !a.printer_id).length;
    if (printers.length < 2 && !systemOpen) { state.alertPrinter = ''; return; }
    const chip = (value, label, n) => {
      const b = el('button', 'chip');
      b.type = 'button';
      b.setAttribute('aria-pressed', String(state.alertPrinter === value));
      b.appendChild(el('span', '', label));
      if (n) b.appendChild(el('span', 'chip-count', n));
      b.addEventListener('click', () => { state.alertPrinter = value; renderAlerts(); });
      root.appendChild(b);
    };
    chip('', 'Tất cả', open.length);
    printers.forEach((p) => chip(p.printer_id, printerName(p), open.filter((a) => a.printer_id === p.printer_id).length));
    if (systemOpen || state.alertPrinter === '_system') chip('_system', 'Hệ thống', systemOpen);
  }

  function renderAlerts() {
    const all = state.data.alerts || [];
    const open = all.filter((a) => !a.acknowledged);
    const count = $('alerts-count');
    count.hidden = open.length === 0;
    count.textContent = String(open.length);
    renderAlertChips(open);

    let list = state.alertFilter === 'open' ? open : all;
    if (state.alertPrinter === '_system') list = list.filter((a) => !a.printer_id);
    else if (state.alertPrinter) list = list.filter((a) => a.printer_id === state.alertPrinter);

    const root = $('alerts');
    root.textContent = '';
    const empty = $('alerts-empty');
    empty.hidden = list.length > 0;
    empty.textContent = state.alertFilter === 'open' ? 'Không có cảnh báo cần xử lý.' : 'Chưa có cảnh báo nào.';
    const names = printerNames();
    const showPrinter = !state.alertPrinter || state.alertPrinter === '_system';
    list.slice(0, 50).forEach((a) => root.appendChild(alertItem(a, names, showPrinter)));
  }

  // ---------------------------------------------------------------------------
  // Activity log
  // ---------------------------------------------------------------------------
  const CATEGORY = { printer: 'Máy in', alert: 'Cảnh báo', notify: 'Thông báo', ai: 'AI', settings: 'Cài đặt', device: 'Thiết bị', system: 'Hệ thống' };

  function eventItem(e, names, compact = false) {
    const li = el('li', 'event');
    li.dataset.level = e.level;
    li.dataset.category = e.category;
    li.appendChild(el('span', 'event-time', compact ? clock(e.created_at) : clock(e.created_at, true)));
    li.appendChild(el('span', 'event-dot'));
    const body = el('div', 'event-msg');
    body.appendChild(document.createTextNode(e.message));
    const meta = [CATEGORY[e.category] || e.category];
    if (!compact && e.printer_id) meta.push(names.get(e.printer_id) || e.printer_id);
    if (e.actor) meta.push(`bởi ${e.actor}`);
    body.appendChild(el('span', 'event-meta', meta.join(' · ')));
    li.appendChild(body);
    return li;
  }

  function eventQuery(before) {
    const q = new URLSearchParams({ limit: '30' });
    const printer = $('ev-printer').value;
    const category = $('ev-category').value;
    if (printer) q.set('printer', printer);
    if (category) q.set('category', category);
    if ($('ev-problems').checked) q.set('level', 'problems');
    if (before) q.set('before', String(before));
    return `/api/events?${q}`;
  }

  async function loadEvents(reset) {
    const ev = state.events;
    if (ev.loading) return;
    if (!reset && !ev.next) return;
    ev.loading = true;
    try {
      const data = await (await api(eventQuery(reset ? null : ev.next))).json();
      ev.items = reset ? data.events : ev.items.concat(data.events);
      ev.next = data.next;
      ev.pagedBeyondFirst = !reset;
      renderEvents();
    } catch (err) {
      handleError(err);
    } finally {
      ev.loading = false;
    }
  }

  function renderEvents() {
    const root = $('events');
    root.textContent = '';
    const names = printerNames();
    state.events.items.forEach((e) => root.appendChild(eventItem(e, names)));
    $('events-empty').hidden = state.events.items.length > 0;
    $('events-more').hidden = !state.events.next;
  }

  function syncEventPrinterOptions() {
    const sel = $('ev-printer');
    const current = sel.value;
    [...sel.options].slice(2).forEach((o) => o.remove());
    (state.data.printers || []).forEach((p) => {
      const o = el('option', '', printerName(p));
      o.value = p.printer_id;
      sel.appendChild(o);
    });
    sel.value = [...sel.options].some((o) => o.value === current) ? current : '';
  }

  function renderAnalyses() {
    const items = state.data.analyses || [];
    const root = $('analyses');
    const openIds = new Set([...root.querySelectorAll('details[open]')].map((d) => d.dataset.id));
    root.textContent = '';
    $('analyses-empty').hidden = items.length > 0;

    items.slice(0, 20).forEach((a) => {
      const li = el('li');
      const det = el('details', 'analysis');
      det.dataset.id = a.id;
      det.open = openIds.has(a.id);
      const sum = el('summary');
      const left = el('div');
      left.style.minWidth = '0';
      left.appendChild(el('div', 'analysis-name', a.filename || 'Model'));
      const changes = Array.isArray(a.changes) ? a.changes : [];
      const meta = [OBJECTIVE[a.objective] || a.objective, TRIGGER[a.trigger] || a.trigger, relTime(a.created_at)].filter(Boolean).join(' · ');
      left.appendChild(el('div', 'analysis-meta', meta));
      const tag = a.status === 'error'
        ? el('span', 'tag tag-danger', 'Lỗi')
        : el('span', changes.length ? 'tag tag-ok' : 'tag', changes.length ? `${changes.length} thay đổi` : 'Giữ nguyên');
      sum.append(left, tag);
      det.appendChild(sum);

      const body = el('div', 'analysis-body');
      if (a.summary) body.appendChild(el('p', 'analysis-summary', a.summary));
      if (changes.length) {
        const table = el('table', 'changes');
        const head = el('tr');
        ['Thông số', 'Giá trị', 'Lý do'].forEach((h) => head.appendChild(el('th', '', h)));
        table.appendChild(head);
        changes.forEach((c) => {
          if (!c || typeof c !== 'object') return;
          const tr = el('tr');
          const k = el('td');
          k.appendChild(el('code', '', c.key || '?'));
          const v = el('td');
          if (c.old !== undefined && c.old !== null && c.old !== '') {
            v.appendChild(el('span', 'change-old num', String(c.old)));
            v.appendChild(document.createTextNode(' → '));
          }
          v.appendChild(el('span', 'change-new num', c.value !== undefined ? String(c.value) : '?'));
          tr.append(k, v, el('td', '', c.reason || ''));
          table.appendChild(tr);
        });
        body.appendChild(table);
      }
      const warns = (Array.isArray(a.warnings) ? a.warnings : []).filter((w) => typeof w === 'string');
      if (warns.length) {
        const ul = el('ul', 'warn-list');
        warns.forEach((w) => ul.appendChild(el('li', '', w)));
        body.appendChild(ul);
      }
      det.appendChild(body);
      li.appendChild(det);
      root.appendChild(li);
    });
  }

  // ---------------------------------------------------------------------------
  // Detail dialog
  // ---------------------------------------------------------------------------
  function openDetail(printerId) {
    if (state.detailPrinterId !== printerId) ctlStatus('');
    state.detailPrinterId = printerId;
    state.detailSnapshotId = null;
    renderDetail();
    const dlg = $('detail');
    if (!dlg.open) dlg.showModal();
  }

  // ---------------------------------------------------------------------------
  // Printer controls
  // ---------------------------------------------------------------------------
  const SPEED = { 1: 'Êm', 2: 'Chuẩn', 3: 'Thể thao', 4: 'Siêu tốc' };
  const CMD_LABEL = { pause: 'Tạm dừng', resume: 'Tiếp tục', stop: 'Dừng in' };

  function ctlStatus(text, kind = '') {
    const n = $('ctl-status');
    n.textContent = text;
    n.dataset.state = kind;
  }

  function renderControls(p) {
    const status = printerStatus(p);
    const t = p.telemetry || {};
    const busy = Boolean(state.cmd);
    const allowed = {
      pause: status === 'printing',
      resume: status === 'paused',
      stop: ['printing', 'paused', 'error'].includes(status),
    };
    document.querySelectorAll('#detail [data-cmd]').forEach((b) => {
      const cmd = b.dataset.cmd;
      let enabled = status !== 'offline' && !busy;
      if (cmd in allowed) enabled = enabled && allowed[cmd];
      if (cmd === 'speed') {
        enabled = enabled && (status === 'printing' || status === 'paused');
        b.setAttribute('aria-pressed', String(Number(b.dataset.level) === t.speed_level));
      }
      b.disabled = !enabled;
    });
    const light = $('ctl-light');
    light.textContent = t.light === 'on' ? 'Tắt đèn' : 'Bật đèn';
    light.dataset.mode = t.light === 'on' ? 'off' : 'on';
    if (!busy && status === 'offline') ctlStatus('Máy in mất kết nối, không thể điều khiển.', 'error');
  }

  async function runCommand(button) {
    const p = (state.data.printers || []).find((x) => x.printer_id === state.detailPrinterId);
    if (!p || state.cmd) return;
    const command = button.dataset.cmd;
    const params = {};
    let label = CMD_LABEL[command] || command;
    if (command === 'light') { params.mode = button.dataset.mode; label = params.mode === 'on' ? 'Bật đèn' : 'Tắt đèn'; }
    if (command === 'speed') { params.level = Number(button.dataset.level); label = `Tốc độ ${SPEED[params.level]}`; }
    if (command === 'stop' && !window.confirm(`Dừng hẳn bản in trên ${printerName(p)}? Không thể tiếp tục sau khi dừng.`)) return;

    state.cmd = { printerId: p.printer_id, id: null, label };
    renderControls(p);
    ctlStatus(`${label}: đang gửi tới cầu nối…`);
    try {
      const { status, data } = await postJsonAllowError(`/api/printers/${encodeURIComponent(p.printer_id)}/commands`, { command, params });
      if (status !== 202) { ctlStatus(data.error || 'Không gửi được lệnh', 'error'); return; }
      state.cmd.id = data.id;
      const deadline = Date.now() + 45000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 1500));
        const st = await (await api(`/api/commands/${encodeURIComponent(data.id)}`)).json();
        if (st.status === 'sent') ctlStatus(`${label}: máy in đang xử lý…`);
        if (st.status === 'done') { ctlStatus(`${label}: máy in đã thực hiện.`, 'ok'); sync(); return; }
        if (st.status === 'failed') { ctlStatus(`${label}: máy in từ chối (${st.result || 'không rõ'}).`, 'error'); return; }
        if (st.status === 'expired') { ctlStatus(`${label}: cầu nối không nhận lệnh. Kiểm tra Redmi.`, 'error'); return; }
      }
      ctlStatus(`${label}: quá thời gian chờ phản hồi.`, 'error');
    } catch (err) {
      if (err instanceof AuthError) handleError(err);
      else ctlStatus('Lỗi kết nối máy chủ', 'error');
    } finally {
      state.cmd = null;
      const cur = (state.data.printers || []).find((x) => x.printer_id === state.detailPrinterId);
      if (cur) renderControls(cur);
    }
  }

  async function loadDetailEvents(printerId) {
    try {
      const data = await (await api(`/api/events?printer=${encodeURIComponent(printerId)}&limit=12`)).json();
      if (state.detailPrinterId !== printerId) return;
      const root = $('detail-events');
      root.textContent = '';
      const names = printerNames();
      data.events.forEach((e) => root.appendChild(eventItem(e, names, true)));
      $('detail-events-empty').hidden = data.events.length > 0;
    } catch { /* non-critical */ }
  }

  function renderDetail() {
    const dlg = $('detail');
    const p = (state.data.printers || []).find((x) => x.printer_id === state.detailPrinterId);
    if (!p) { if (dlg.open) dlg.close(); return; }
    const t = p.telemetry || {};
    const snaps = snapshotsOf(p.printer_id);
    const current = snaps.find((s) => s.id === state.detailSnapshotId) || snaps[0];
    const status = printerStatus(p);

    $('detail-title').textContent = printerName(p);
    $('detail-sub').textContent = [p.model, STATUS[status], t.job_name].filter(Boolean).join(' · ');

    const img = $('detail-img');
    if (current) {
      const at = current.captured_at || current.created_at;
      img.alt = `Ảnh camera lúc ${clock(at, true)}`;
      if (img.dataset.id !== current.id) setImage(img, current.id);
      $('detail-caption').textContent = `${clock(at, true)} · ${relTime(at)}`;
    } else {
      img.removeAttribute('src');
      delete img.dataset.id;
      $('detail-caption').textContent = 'Chưa có ảnh';
    }

    let dv = vision(current);
    if ((status === 'idle' || status === 'offline') && current === snaps[0] && current
        && ageSec(current.captured_at || current.created_at) > STALE_IMAGE_SEC) {
      dv = { state: 'none', text: 'Máy không in nên camera tạm nghỉ. Ảnh sẽ tự cập nhật khi bắt đầu in.' };
    }
    const fresh = visionBox(dv);
    fresh.id = 'detail-vision';
    $('detail-vision').replaceWith(fresh);

    // Older snapshots show the numbers recorded with them; newest shows live values
    const tv = current && current !== snaps[0] ? (current.telemetry || {}) : t;
    const kv = $('detail-metrics');
    kv.textContent = '';
    const tempPair = (v, target) => (v !== undefined ? `${temp(v)}C${target ? ` → ${temp(target)}C` : ''}` : '--');
    [
      ['Trạng thái', STATUS[status]],
      ['Tiến độ', typeof tv.progress === 'number' ? `${tv.progress}%` : '--'],
      ['Thời gian còn lại', status === 'printing' && tv.remaining_min !== undefined ? minutes(tv.remaining_min).replace('còn ', '') : '--'],
      ['Lớp', tv.current_layer !== undefined ? `${tv.current_layer} / ${tv.total_layers ?? '?'}` : '--'],
      ['Đầu in', tempPair(tv.nozzle_temp, tv.nozzle_target)],
      ['Bàn in', tempPair(tv.bed_temp, tv.bed_target)],
      ['Quạt làm mát', tv.fan_speed !== undefined ? `${tv.fan_speed}%` : '--'],
      ['Cầu nối', p.device_id || '--'],
      ['Cập nhật', relTime(p.last_seen)],
    ].forEach(([k, v]) => { kv.appendChild(el('dt', '', k)); kv.appendChild(el('dd', 'num', v)); });

    renderControls(p);

    const names = printerNames();
    const dAlerts = $('detail-alerts');
    dAlerts.textContent = '';
    const mine = openAlertsFor(p.printer_id);
    mine.slice(0, 20).forEach((a) => dAlerts.appendChild(alertItem(a, names, false)));
    $('detail-alerts-empty').hidden = mine.length > 0;
    loadDetailEvents(p.printer_id);

    const thumbs = $('detail-thumbs');
    thumbs.textContent = '';
    snaps.slice(0, 30).forEach((s) => {
      const b = el('button', 'thumb');
      b.type = 'button';
      b.setAttribute('role', 'option');
      b.setAttribute('aria-selected', String(Boolean(current && s.id === current.id)));
      const at = s.captured_at || s.created_at;
      b.setAttribute('aria-label', `Ảnh lúc ${clock(at, true)}`);
      const r = s.vision_result;
      if (r && typeof r === 'object' && (r.ok === false || (Array.isArray(r.issues) && r.issues.length))) b.dataset.flag = 'warn';
      const ti = el('img');
      ti.alt = '';
      setImage(ti, s.id);
      b.append(ti, el('span', 'num', clock(at)));
      b.addEventListener('click', () => { state.detailSnapshotId = s.id; renderDetail(); });
      thumbs.appendChild(b);
    });
  }

  // ---------------------------------------------------------------------------
  // Settings dialog
  // ---------------------------------------------------------------------------
  const SECRET_FIELDS = ['ai_key', 'telegram_bot_token'];
  let loadedSettings = null;

  function formatBytes(n) {
    if (!n) return '0 MB';
    return n >= 1073741824 ? `${(n / 1073741824).toFixed(1)} GB` : `${(n / 1048576).toFixed(1)} MB`;
  }
  function renderStorage(st) {
    if (!st) return;
    $('storage-info').textContent = `${st.snapshot_count} ảnh · ${formatBytes(st.snapshot_bytes)}`
      + (st.oldest_snapshot ? ` · cũ nhất ${relTime(st.oldest_snapshot)}` : '');
  }
  function result(name, text, kind = '') {
    const node = document.querySelector(`[data-result="${name}"]`) || $('settings-status');
    node.textContent = text;
    node.dataset.state = kind;
  }
  function ensureOption(select, value) {
    if (![...select.options].some((o) => o.value === String(value))) {
      const o = el('option', '', String(value));
      o.value = String(value);
      select.appendChild(o);
    }
    select.value = String(value);
  }

  function fillSettings(st) {
    loadedSettings = st;
    const f = $('settings-form');
    ['ai_base_url', 'ai_model', 'vision_model', 'telegram_chat_id'].forEach((k) => { f.elements[k].value = st[k] || ''; });
    f.elements.vision_enabled.checked = Boolean(st.vision_enabled);
    f.elements.capture_when_idle.checked = Boolean(st.capture_when_idle);
    ensureOption(f.elements.snapshot_interval_seconds, st.snapshot_interval_seconds);
    ensureOption(f.elements.snapshot_retention_hours, st.snapshot_retention_hours);
    SECRET_FIELDS.forEach((k) => {
      const input = f.elements[k];
      input.value = '';
      input.placeholder = st[`${k}_set`] ? 'Đã lưu, để trống nếu giữ nguyên' : 'Chưa đặt';
    });
  }

  // Only fields the user actually changed; secrets only when typed
  function settingsPatch() {
    const f = $('settings-form');
    const st = loadedSettings || {};
    const patch = {};
    ['ai_base_url', 'ai_model', 'vision_model', 'telegram_chat_id'].forEach((k) => {
      const v = f.elements[k].value.trim();
      if (v !== (st[k] || '')) patch[k] = v;
    });
    ['vision_enabled', 'capture_when_idle'].forEach((k) => {
      if (f.elements[k].checked !== Boolean(st[k])) patch[k] = f.elements[k].checked;
    });
    ['snapshot_interval_seconds', 'snapshot_retention_hours'].forEach((k) => {
      const v = parseInt(f.elements[k].value, 10);
      if (v !== st[k]) patch[k] = v;
    });
    SECRET_FIELDS.forEach((k) => {
      const v = f.elements[k].value.trim();
      if (v) patch[k] = v;
    });
    return patch;
  }

  async function postJson(path, body) {
    const res = await api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    return res.json();
  }
  async function postJsonAllowError(path, body) {
    // 400 responses carry a user-facing message
    const headers = new Headers({ 'Content-Type': 'application/json', 'X-PrintPilot-Request': '1' });
    if (state.token) headers.set('Authorization', `Bearer ${state.token}`);
    const res = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body || {}), cache: 'no-store', credentials: 'same-origin' });
    if (res.status === 401 || res.status === 403) throw new AuthError('unauthorized');
    const data = await res.json().catch(() => ({}));
    return { status: res.status, data };
  }

  async function saveSettings() {
    const patch = settingsPatch();
    if (!Object.keys(patch).length) return true;
    const { status, data } = await postJsonAllowError('/api/settings', patch);
    if (status !== 200) {
      result('settings', data.error || 'Không lưu được', 'error');
      return false;
    }
    fillSettings(data.settings);
    result('settings', 'Đã lưu', 'ok');
    sync();
    return true;
  }

  async function openSettings() {
    const dlg = $('settings');
    $('chat-choices').hidden = true;
    document.querySelectorAll('.action-result').forEach((n) => { n.textContent = ''; n.dataset.state = ''; });
    try {
      const data = await (await api('/api/settings')).json();
      fillSettings(data.settings);
      renderStorage(data.storage);
      if (!dlg.open) dlg.showModal();
    } catch (err) {
      handleError(err);
    }
  }

  function bindSettings() {
    const dlg = $('settings');
    $('btn-settings').addEventListener('click', openSettings);
    dlg.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => dlg.close()));
    $('settings-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('settings-save');
      btn.disabled = true;
      try { await saveSettings(); } catch (err) { handleError(err); } finally { btn.disabled = false; }
    });

    const busy = async (btn, fn) => {
      btn.disabled = true;
      try { await fn(); } catch (err) { if (err instanceof AuthError) handleError(err); else result(btn.dataset.action, 'Lỗi kết nối', 'error'); } finally { btn.disabled = false; }
    };
    dlg.querySelector('[data-action="test-ai"]').addEventListener('click', (e) => busy(e.currentTarget, async () => {
      result('test-ai', 'Đang lưu và kiểm tra…');
      if (!(await saveSettings())) { result('test-ai', ''); return; }
      const { status, data } = await postJsonAllowError('/api/settings/test-ai', {});
      if (status === 200 && data.ok) result('test-ai', `Kết nối tốt (${data.latency_ms} ms)`, 'ok');
      else result('test-ai', data.error || 'Không kết nối được AI', 'error');
    }));
    dlg.querySelector('[data-action="find-chats"]').addEventListener('click', (e) => busy(e.currentTarget, async () => {
      const f = $('settings-form');
      const list = $('chat-choices');
      list.textContent = '';
      result('telegram', 'Đang tìm…');
      const { status, data } = await postJsonAllowError('/api/settings/telegram-chats', { telegram_bot_token: f.elements.telegram_bot_token.value.trim() });
      if (status !== 200 || !data.ok) { result('telegram', data.error || 'Không đọc được từ Telegram', 'error'); return; }
      if (!data.chats.length) { result('telegram', 'Chưa thấy tin nhắn nào. Hãy nhắn cho bot một tin rồi thử lại.', 'error'); list.hidden = true; return; }
      result('telegram', 'Chọn chat nhận cảnh báo:');
      data.chats.forEach((c) => {
        const li = el('li');
        const b = el('button', '', `${c.name} · ${c.id}`);
        b.type = 'button';
        b.addEventListener('click', () => { f.elements.telegram_chat_id.value = c.id; list.hidden = true; result('telegram', 'Đã chọn, bấm Lưu để áp dụng.', 'ok'); });
        li.appendChild(b);
        list.appendChild(li);
      });
      list.hidden = false;
    }));
    dlg.querySelector('[data-action="test-telegram"]').addEventListener('click', (e) => busy(e.currentTarget, async () => {
      const f = $('settings-form');
      result('telegram', 'Đang gửi…');
      const { status, data } = await postJsonAllowError('/api/settings/test-telegram', {
        telegram_bot_token: f.elements.telegram_bot_token.value.trim(),
        telegram_chat_id: f.elements.telegram_chat_id.value.trim(),
      });
      if (status === 200 && data.ok) result('telegram', 'Đã gửi, kiểm tra Telegram.', 'ok');
      else result('telegram', data.error || 'Không gửi được', 'error');
    }));
    dlg.querySelector('[data-action="cleanup"]').addEventListener('click', (e) => busy(e.currentTarget, async () => {
      if (!(await saveSettings())) return;
      const data = await postJson('/api/maintenance/cleanup', {});
      renderStorage(data.storage);
      const n = (data.removed.snapshots || 0) + (data.removed.orphans || 0);
      result('cleanup', n ? `Đã xóa ${n} ảnh` : 'Không có ảnh hết hạn', 'ok');
      sync();
    }));
  }

  // ---------------------------------------------------------------------------
  // Sync + auth
  // ---------------------------------------------------------------------------
  function setSync(text, isError = false) {
    const s = $('sync-status');
    s.textContent = text;
    s.dataset.state = isError ? 'error' : 'ok';
  }
  function showBanner(text) { const b = $('banner'); b.textContent = text; b.hidden = !text; }

  function handleError(err) {
    if (err instanceof AuthError) {
      state.token = '';
      store.del(TOKEN_KEY);
      showLogin('Phiên đăng nhập không hợp lệ. Vui lòng đăng nhập lại.');
      return;
    }
    setSync('Mất kết nối', true);
    showBanner('Không kết nối được máy chủ PrintPilot. Đang thử lại…');
  }

  async function sync() {
    if (state.syncing) return;
    state.syncing = true;
    try {
      const data = await (await api('/api/state')).json();
      state.data = {
        printers: Array.isArray(data.printers) ? data.printers : [],
        snapshots: Array.isArray(data.snapshots) ? data.snapshots : [],
        alerts: Array.isArray(data.alerts) ? data.alerts : [],
        analyses: Array.isArray(data.analyses) ? data.analyses : [],
        devices: Array.isArray(data.devices) ? data.devices : [],
        config: data.config || {},
      };
      showBanner('');
      setSync(`Cập nhật ${clock(new Date().toISOString())}`);
      renderHealth();
      renderPrinters();
      renderAlerts();
      renderAnalyses();
      syncEventPrinterOptions();
      if (!state.events.pagedBeyondFirst) loadEvents(true);
      if ($('detail').open) renderDetail();
      pruneImages(new Set(state.data.snapshots.map((s) => s.id)));
    } catch (err) {
      handleError(err);
    } finally {
      state.syncing = false;
    }
  }

  function schedule() {
    clearInterval(state.timer);
    state.timer = setInterval(() => { if (!document.hidden) sync(); }, REFRESH_MS);
  }

  function renderUser() {
    const chip = $('user-chip');
    $('btn-settings').hidden = !(state.tsUser || state.token);
    if (state.tsUser) {
      chip.textContent = state.tsUser;
      chip.title = 'Đăng nhập bằng tài khoản Tailscale';
      chip.hidden = false;
      $('btn-logout').hidden = true;
    } else {
      chip.textContent = 'Mã quản lý';
      chip.hidden = !state.token;
      $('btn-logout').hidden = !state.token;
    }
  }

  function showLogin(message = '') {
    clearInterval(state.timer);
    renderUser();
    const err = $('login-error');
    err.textContent = message;
    err.hidden = !message;
    const dlg = $('login');
    if (!dlg.open) dlg.showModal();
    $('login-token').focus();
  }

  async function detectTailscale() {
    try {
      const res = await fetch('/api/whoami', { headers: { 'X-PrintPilot-Request': '1' }, cache: 'no-store' });
      if (!res.ok) return;
      const who = await res.json();
      if (who && who.authenticated && who.method === 'tailscale' && typeof who.user === 'string') state.tsUser = who.user;
    } catch { /* older server or offline */ }
  }

  function bind() {
    bindSettings();
    ['ev-printer', 'ev-category', 'ev-problems'].forEach((id) => $(id).addEventListener('change', () => loadEvents(true)));
    $('events-more').addEventListener('click', () => loadEvents(false));
    document.querySelectorAll('.seg[data-filter]').forEach((b) => b.addEventListener('click', () => {
      state.alertFilter = b.dataset.filter;
      document.querySelectorAll('.seg[data-filter]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
      renderAlerts();
    }));

    const detail = $('detail');
    detail.querySelector('[data-close]').addEventListener('click', () => detail.close());
    detail.addEventListener('click', (e) => { if (e.target === detail) detail.close(); });
    detail.addEventListener('close', () => { state.detailPrinterId = null; });
    detail.querySelectorAll('[data-cmd]').forEach((b) => b.addEventListener('click', () => runCommand(b)));

    const login = $('login');
    login.addEventListener('cancel', (e) => e.preventDefault()); // must authenticate
    $('login-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const value = $('login-token').value.trim();
      if (!value) return;
      const btn = $('login-submit');
      btn.disabled = true;
      const previous = state.token;
      state.token = value;
      try {
        await api('/api/state');
        store.set(TOKEN_KEY, value);
        $('login-token').value = '';
        login.close();
        renderUser();
        await sync();
        schedule();
      } catch (err) {
        state.token = previous;
        $('login-error').textContent = err instanceof AuthError ? 'Mã không đúng.' : 'Không kết nối được máy chủ.';
        $('login-error').hidden = false;
      } finally {
        btn.disabled = false;
      }
    });

    $('btn-logout').addEventListener('click', () => {
      state.token = '';
      store.del(TOKEN_KEY);
      showLogin();
    });

    document.addEventListener('visibilitychange', () => { if (!document.hidden && !$('login').open) sync(); });
  }

  async function init() {
    bind();
    await detectTailscale();
    renderUser();
    if (!state.tsUser && !state.token) {
      setSync('Chưa đăng nhập');
      showLogin();
      return;
    }
    await sync();
    schedule();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
