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
  // Wide screens show printer details docked next to the list instead of a modal
  const WIDE = window.matchMedia('(min-width: 1200px)');
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  const SVG_NS = 'http://www.w3.org/2000/svg';
  // Icons come from the inline sprite in index.html (Lucide, ISC)
  const icon = (name, cls = '') => {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', `i ${cls}`.trim());
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.appendChild(use);
    return svg;
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
    cmds: {},                    // printerId -> label while a control command is in flight
    cmdMsg: {},                  // printerId -> { text, kind, at }: last command feedback
    expect: {},                  // printerId -> { key, value, until }: confirmed command, server may lag
    live: null,                  // { printerId, seq, token, url, frames:[t...] } while watching
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
  const TRIGGER = { manual: 'Bấm AI auto setting', send: 'Trước khi gửi in', upload: 'Tải lên từ dashboard' };

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

  const NOTE_ICON = { ok: 'sparkles', warn: 'triangle-alert', none: 'info', rest: 'camera' };
  function visionBox(v) {
    const box = el('div', 'note');
    box.dataset.state = v.state;
    box.appendChild(icon(NOTE_ICON[v.icon || v.state] || 'info'));
    box.appendChild(el('span', '', v.text));
    return box;
  }

  function stat(iconName, label, value, target) {
    const m = el('div', 'stat');
    const l = el('span', 'stat-label');
    l.title = label;
    l.append(icon(iconName), el('span', '', label));
    const v = el('span', 'stat-value num', value);
    if (target) v.appendChild(el('span', 'stat-target', ` / ${target}`));
    m.append(l, v);
    return m;
  }

  // ---------------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------------
  function renderHealth() {
    const cfg = state.data.config || {};
    const LABEL = { ok: 'Hoạt động', warn: 'Chập chờn', error: 'Chưa cấu hình', off: 'Tắt' };
    const set = (id, s, title) => { const n = $(id); n.dataset.state = s; n.dataset.label = LABEL[s] || ''; n.title = title; };
    set('health-ai', cfg.ai_configured ? 'ok' : 'error', cfg.ai_configured ? 'AI sẵn sàng' : 'Chưa cấu hình AI');
    const devices = state.data.devices || [];
    const live = devices.filter((d) => !d.stale);
    set('health-bridge', !devices.length ? 'off' : live.length === devices.length ? 'ok' : live.length ? 'warn' : 'error',
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
      ? `${printers.length} máy · ${active ? `${active} đang in` : 'không máy nào đang in'}`
      : '';

    const overview = $('fleet-summary'); overview.textContent = '';
    [['Máy kết nối', printers.filter((p) => printerStatus(p) !== 'offline').length], ['Đang in', active], ['Sẵn sàng', printers.filter((p) => printerStatus(p) === 'idle').length], ['Cần chú ý', printers.filter((p) => ['error', 'offline'].includes(printerStatus(p)) || openAlertsFor(p.printer_id).length).length]].forEach(([label, count]) => { const item = el('div'); item.append(el('b', 'num', String(count).padStart(2, '0')), el('span', '', label)); overview.appendChild(item); });
    $('fleet-count').textContent = `${printers.length} thiết bị`;
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
    markSelected();
    const navCount = $('nav-printers-count');
    navCount.hidden = !printers.length;
    navCount.textContent = String(printers.length);

    // Docked layout always shows one printer: keep the selection valid
    if (WIDE.matches && printers.length && !printers.some((p) => p.printer_id === state.detailPrinterId)) {
      openDetail(printers[0].printer_id);
    }
  }

  function markSelected() {
    const docked = WIDE.matches && $('detail').open;
    document.querySelectorAll('#printers .printer').forEach((c) => {
      c.classList.toggle('selected', docked && c.dataset.printer === state.detailPrinterId);
    });
  }

  function buildPrinterCard(printerId) {
    const card = el('article', 'printer');
    card.dataset.printer = printerId;
    // Whole-card button opens details; the live button sits above it
    const open = el('button', 'printer-open');
    open.type = 'button';
    open.addEventListener('click', () => openDetail(printerId));

    const media = el('div', 'printer-media');
    const img = el('img');
    img.alt = '';
    const empty = el('div', 'media-empty');
    empty.append(icon('image-off'), el('span', 'media-empty-text', 'Chưa có ảnh'));
    const status = el('span', 'glass media-status');
    status.append(el('span', 'status-dot'), el('span', 'status-text'));
    const age = el('span', 'glass media-age num');
    const actions = el('div', 'media-actions');
    const liveBtn = el('button', 'live-btn');
    liveBtn.type = 'button';
    liveBtn.append(icon('video'), el('span', '', 'Trực tiếp'));
    liveBtn.addEventListener('click', (e) => { e.stopPropagation(); openDetail(printerId, true); });
    actions.appendChild(liveBtn);
    const line = el('div', 'progress-line');
    line.appendChild(el('span'));
    line.setAttribute('role', 'progressbar');
    line.setAttribute('aria-valuemin', '0');
    line.setAttribute('aria-valuemax', '100');
    media.append(img, empty, el('div', 'media-shade'), status, age, actions, line);

    const body = el('div', 'printer-body');
    const head = el('div', 'printer-head');
    const titles = el('div', 'printer-titles');
    const nameRow = el('div', 'printer-name-row');
    const count = el('span', 'alert-count num');
    count.hidden = true;
    nameRow.append(el('span', 'printer-name'), count);
    titles.append(nameRow, el('div', 'printer-job'));
    const prog = el('div', 'printer-progress');
    prog.append(el('span', 'progress-pct num'), el('span', 'progress-eta num'));
    head.append(titles, prog);
    body.append(head, el('div', 'stats'), buildCardActions(printerId), el('div', 'vision-slot'));

    card.append(open, media, body);
    return card;
  }

  function iconButton(cls, label, iconName, extra = {}) {
    const b = el('button', cls);
    b.type = 'button';
    b.setAttribute('aria-label', label);
    b.dataset.tip = label;
    Object.assign(b.dataset, extra);
    if (iconName) b.appendChild(icon(iconName));
    return b;
  }

  function buildCardActions(printerId) {
    const row = el('div', 'card-actions');
    const toggle = iconButton('card-btn', 'Tạm dừng', null, { cmd: 'pause', toggle: 'pause' });
    const pause = icon('pause'); pause.dataset.icon = 'pause';
    const play = icon('play'); play.dataset.icon = 'play'; play.setAttribute('hidden', '');
    toggle.append(pause, play);
    const stop = iconButton('card-btn card-danger', 'Dừng in', 'octagon-x', { cmd: 'stop' });
    const light = iconButton('card-btn', 'Bật đèn', null, { cmd: 'light' });
    const on = icon('lightbulb'); on.dataset.light = 'on';
    const off = icon('lightbulb-off'); off.dataset.light = 'off'; off.setAttribute('hidden', '');
    light.append(on, off);
    const more = iconButton('card-btn card-more', 'Chi tiết', 'maximize-2');
    more.addEventListener('click', (e) => { e.stopPropagation(); openDetail(printerId); });
    [toggle, stop, light].forEach((b) => b.addEventListener('click', (e) => { e.stopPropagation(); runCommand(b, printerId); }));
    const msg = el('span', 'card-msg');
    msg.setAttribute('role', 'status');
    row.append(toggle, stop, light, msg, more);
    return row;
  }

  function updatePrinterCard(card, p) {
    const t = p.telemetry || {};
    const status = printerStatus(p);
    const snaps = snapshotsOf(p.printer_id);
    const latest = snaps[0];
    const name = printerName(p);

    card.querySelector('.printer-open').setAttribute('aria-label', `${name}: ${STATUS[status]}. Mở chi tiết`);
    const liveBtn = card.querySelector('.live-btn');
    liveBtn.disabled = status === 'offline';
    liveBtn.setAttribute('aria-label', `Xem camera trực tiếp ${name}`);
    card.dataset.status = status;

    const media = card.querySelector('.printer-media');
    const img = media.querySelector('img');
    const empty = media.querySelector('.media-empty');
    const age = media.querySelector('.media-age');
    if (latest) {
      const capturedAt = latest.captured_at || latest.created_at;
      const stale = ageSec(capturedAt) > STALE_IMAGE_SEC;
      media.dataset.stale = String(stale);
      img.alt = `Camera ${name} lúc ${clock(capturedAt)}`;
      empty.hidden = true;
      if (img.dataset.id !== latest.id) {
        setImage(img, latest.id, () => { empty.hidden = false; empty.querySelector('.media-empty-text').textContent = 'Không tải được ảnh'; });
      }
      age.textContent = `${clock(capturedAt)} · ${relTime(capturedAt)}`;
      age.hidden = false;
    } else {
      img.removeAttribute('src');
      delete img.dataset.id;
      empty.hidden = false;
      age.hidden = true;
    }
    media.querySelector('.media-status').dataset.status = status;
    media.querySelector('.status-text').textContent = STATUS[status];

    card.querySelector('.printer-name').textContent = name;
    const openAlerts = openAlertsFor(p.printer_id).length;
    const countEl = card.querySelector('.alert-count');
    countEl.hidden = openAlerts === 0;
    countEl.textContent = '';
    countEl.append(icon('bell'), document.createTextNode(String(openAlerts)));
    countEl.title = `${openAlerts} cảnh báo chưa xử lý`;

    const job = card.querySelector('.printer-job');
    job.textContent = t.job_name || [p.model, `cập nhật ${relTime(p.last_seen)}`].filter(Boolean).join(' · ');
    job.title = t.job_name || '';

    const hasProgress = typeof t.progress === 'number';
    const pct = hasProgress ? Math.min(100, Math.max(0, t.progress)) : 0;
    const line = media.querySelector('.progress-line');
    line.hidden = !hasProgress;
    line.dataset.state = status;
    line.dataset.done = String(pct >= 100);
    line.setAttribute('aria-valuenow', String(pct));
    line.firstChild.style.width = `${pct}%`;
    const progBox = card.querySelector('.printer-progress');
    progBox.hidden = !hasProgress;
    card.querySelector('.progress-pct').textContent = `${pct}%`;
    card.querySelector('.progress-eta').textContent = status === 'printing' ? minutes(t.remaining_min) : (pct >= 100 ? 'Đã xong' : STATUS[status]);

    const stats = card.querySelector('.stats');
    stats.textContent = '';
    stats.append(
      stat('flame', 'Đầu in', temp(t.nozzle_temp), t.nozzle_target ? temp(t.nozzle_target) : ''),
      stat('thermometer', 'Bàn in', temp(t.bed_temp), t.bed_target ? temp(t.bed_target) : ''),
      stat('layers', 'Lớp', t.current_layer !== undefined ? `${t.current_layer}/${t.total_layers ?? '?'}` : '--'),
      stat('fan', 'Quạt', t.fan_speed !== undefined ? `${t.fan_speed}%` : '--'),
    );

    applyControls(card, p);
    renderCardMsg(card, p.printer_id);

    const slot = card.querySelector('.vision-slot');
    slot.textContent = '';
    const resting = (status === 'idle' || status === 'offline') && latest
      && ageSec(latest.captured_at || latest.created_at) > STALE_IMAGE_SEC;
    let v = vision(latest);
    if (p.last_error) v = { state: 'warn', text: p.last_error };
    else if (resting) v = { state: 'none', icon: 'rest', text: 'Máy không in nên camera tạm nghỉ, ảnh tự cập nhật khi bắt đầu in.' };
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
    const ic = el('span', 'alert-icon');
    ic.appendChild(icon(a.acknowledged ? 'check' : (a.severity === 'info' ? 'info' : 'triangle-alert')));
    li.appendChild(ic);
    const main = el('div', 'alert-main');
    main.appendChild(el('p', 'alert-msg', a.message || 'Cảnh báo'));
    const source = showPrinter ? (names.get(a.printer_id) || a.printer_id || a.device_id || 'Hệ thống') : '';
    main.appendChild(el('p', 'alert-meta',
      [SEVERITY[a.severity] || a.severity, source, `${clock(a.created_at, true)} (${relTime(a.created_at)})`].filter(Boolean).join(' · ')));
    li.appendChild(main);
    if (a.acknowledged) {
      const acked = el('span', 'alert-acked');
      acked.append(icon('check-check'), el('span', '', a.acked_by ? a.acked_by : 'Đã xử lý'));
      acked.title = a.acked_by ? `Đã xử lý bởi ${a.acked_by}` : 'Đã xử lý';
      li.appendChild(acked);
    } else {
      const btn = el('button', 'ack-btn');
      btn.type = 'button';
      btn.appendChild(icon('check'));
      btn.setAttribute('aria-label', 'Đánh dấu đã xử lý');
      btn.dataset.tip = 'Đã xử lý';
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
    empty.querySelector('.empty-title').textContent = state.alertFilter === 'open' ? 'Không có cảnh báo cần xử lý' : 'Chưa có cảnh báo nào';
    empty.querySelector('.empty-text').textContent = state.alertFilter === 'open' ? 'Mọi máy in đang ổn.' : '';
    const names = printerNames();
    const showPrinter = !state.alertPrinter || state.alertPrinter === '_system';
    list.slice(0, 50).forEach((a) => root.appendChild(alertItem(a, names, showPrinter)));
  }

  // ---------------------------------------------------------------------------
  // Activity log
  // ---------------------------------------------------------------------------
  const CATEGORY = { printer: 'Máy in', alert: 'Cảnh báo', notify: 'Thông báo', ai: 'AI', settings: 'Cài đặt', device: 'Thiết bị', system: 'Hệ thống', filament: 'Nhựa in' };

  const CATEGORY_ICON = { printer: 'printer', alert: 'bell', notify: 'send', ai: 'sparkles', settings: 'settings', device: 'plug', system: 'refresh-cw', filament: 'layers' };

  function dayLabel(value) {
    const d = toDate(value);
    if (!d) return '';
    const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((start(new Date()) - start(d)) / 86400000);
    if (diff === 0) return 'Hôm nay';
    if (diff === 1) return 'Hôm qua';
    return d.toLocaleDateString('vi-VN', { weekday: 'long', day: '2-digit', month: '2-digit' });
  }

  function eventItem(e, names, compact = false) {
    const li = el('li', 'event');
    li.dataset.level = e.level;
    li.dataset.category = e.category;
    li.appendChild(el('span', 'event-time num', clock(e.created_at)));
    const ic = el('span', 'event-icon');
    ic.appendChild(icon(e.level === 'error' || e.level === 'warning' ? 'triangle-alert' : (CATEGORY_ICON[e.category] || 'info')));
    li.appendChild(ic);
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
    let day = null;
    state.events.items.forEach((e) => {
      const label = dayLabel(e.created_at);
      if (label !== day) { day = label; root.appendChild(el('li', 'day', label)); }
      root.appendChild(eventItem(e, names));
    });
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

  const SETTING_LABEL = {
    layer_height: ['Chiều cao lớp', 'mm'],
    initial_layer_print_height: ['Chiều cao lớp đầu', 'mm'],
    wall_loops: ['Số vòng tường', ''],
    top_shell_layers: ['Số lớp vỏ trên', ''],
    bottom_shell_layers: ['Số lớp vỏ dưới', ''],
    sparse_infill_density: ['Mật độ infill', '%'],
    outer_wall_speed: ['Tốc độ tường ngoài', 'mm/s'],
    inner_wall_speed: ['Tốc độ tường trong', 'mm/s'],
    sparse_infill_speed: ['Tốc độ infill', 'mm/s'],
    brim_width: ['Độ rộng brim', 'mm'],
    enable_support: ['Bật support', ''],
    support_type: ['Kiểu support', ''],
    support_threshold_angle: ['Góc bắt đầu support', '°'],
    seam_position: ['Vị trí đường nối', ''],
    sparse_infill_pattern: ['Kiểu infill', ''],
    brim_type: ['Kiểu brim', ''],
    outer_wall_acceleration: ['Gia tốc tường ngoài', 'mm/s²'],
    top_surface_speed: ['Tốc độ mặt trên', 'mm/s'],
    initial_layer_speed: ['Tốc độ lớp đầu', 'mm/s'],
    nozzle_temperature: ['Nhiệt đầu phun', '°C'],
    nozzle_temperature_initial_layer: ['Nhiệt đầu phun lớp đầu', '°C'],
    fan_min_speed: ['Quạt tối thiểu', '%'],
    fan_max_speed: ['Quạt tối đa', '%'],
    overhang_fan_speed: ['Quạt phần treo', '%'],
    slow_down_layer_time: ['Thời gian lớp tối thiểu', 's'],
    textured_plate_temp: ['Nhiệt bàn (Textured PEI)', '°C'],
    textured_plate_temp_initial_layer: ['Nhiệt bàn lớp đầu (Textured PEI)', '°C'],
    hot_plate_temp: ['Nhiệt bàn (PEI trơn)', '°C'],
    hot_plate_temp_initial_layer: ['Nhiệt bàn lớp đầu (PEI trơn)', '°C'],
    cool_plate_temp: ['Nhiệt bàn (Cool Plate)', '°C'],
    cool_plate_temp_initial_layer: ['Nhiệt bàn lớp đầu (Cool Plate)', '°C'],
    eng_plate_temp: ['Nhiệt bàn (Engineering)', '°C'],
    eng_plate_temp_initial_layer: ['Nhiệt bàn lớp đầu (Engineering)', '°C'],
  };
  const FINDING_SEVERITY = {critical: ['Nghiêm trọng', 'tag tag-danger'], warn: ['Cảnh báo', 'tag tag-warn'], info: ['Lưu ý', 'tag']};

  // What the analysis was grounded on: rendered views, rule findings, measured objects
  function buildInsight(a) {
    const ins = a.insights;
    if (!ins || typeof ins !== 'object') return null;
    const box = el('details', 'insight');
    const findings = Array.isArray(ins.findings) ? ins.findings : [];
    const objects = Array.isArray(ins.objects) ? ins.objects.filter((o) => o && !o.empty) : [];
    const head = el('summary');
    const counts = ['critical', 'warn'].map((k) => findings.filter((f) => f.severity === k).length);
    head.append(el('strong', '', 'AI đã căn cứ vào đâu'),
      el('span', 'muted', [objects.length ? `${objects.length} object đã đo` : '', counts[0] ? `${counts[0]} nghiêm trọng` : '', counts[1] ? `${counts[1]} cảnh báo` : '',
        ins.material && ins.material.profile ? ins.material.profile.preset : (ins.material && ins.material.type) || '', ins.printer && ins.printer.model ? `máy ${ins.printer.model}` : ''].filter(Boolean).join(' · ')));
    box.appendChild(head);
    const inner = el('div', 'insight-body');
    if (ins.views) {
      const fig = el('figure', 'insight-views');
      const img = el('img'); img.alt = 'Các góc nhìn model: cam/đỏ là phần treo 45°/60°, xanh dương là mặt chạm bàn';
      fig.append(img, el('figcaption', 'muted', 'Ảnh AI nhận được: cam = treo 45–60°, đỏ = treo trên 60°, xanh dương = mặt chạm bàn; lưới in kích thước thật; số trên ảnh = số object.'));
      inner.appendChild(fig);
      box.addEventListener('toggle', () => { if (box.open && !img.src) assetUrl(ins.views).then((url) => { img.src = url; }).catch(() => { fig.hidden = true; }); });
    }
    if (findings.length) {
      const list = el('ul', 'finding-list');
      findings.forEach((f) => { const sev = FINDING_SEVERITY[f.severity] || FINDING_SEVERITY.info; const li = el('li'); li.append(el('span', sev[1], sev[0]), el('span', '', f.message || f.code)); list.appendChild(li); });
      inner.appendChild(list);
    }
    if (objects.length) {
      const wrap = el('div', 'table-scroll');
      const table = el('table', 'object-table');
      const hr = el('tr');
      ['#', 'Object', 'Kích thước (mm)', 'Chạm bàn', 'Cao/đáy', 'Treo >60°', 'Dày TB'].forEach((h) => hr.appendChild(el('th', '', h)));
      table.appendChild(hr);
      objects.slice(0, 40).forEach((o) => {
        const tr = el('tr');
        [o.label, o.name, (o.size_mm || []).join(' × '), `${o.bed_contact_mm2} mm²`, o.slenderness, `${o.overhang_60_mm2} mm² (${o.overhang_60_pct}%)`, `${o.mean_thickness_mm} mm`]
          .forEach((v) => tr.appendChild(el('td', '', v == null ? '—' : String(v))));
        table.appendChild(tr);
      });
      wrap.appendChild(table); inner.appendChild(wrap);
    }
    const facts = [];
    if (ins.slice) facts.push(`Bản slice: ${ins.slice.time_h} giờ · ${ins.slice.layers} lớp · ${(ins.slice.filaments || []).map((f) => `nhựa #${f.index} ${f.weight_g} g`).join(', ')}${ins.slice.filament_changes ? ` · ${ins.slice.filament_changes} lần đổi màu` : ''}`);
    if (ins.environment) facts.push(`Phòng: ${ins.environment.temperature ?? '—'}°C, độ ẩm ${ins.environment.humidity ?? '—'}%`);
    (ins.spools || []).forEach((sp) => facts.push(`Cuộn «${sp.name}»: còn ${sp.remaining_g ?? '?'} g${sp.days_since_dry != null ? `, ${sp.days_since_dry} ngày từ lần sấy/mở` : ''}`));
    if (facts.length) { const ul = el('ul', 'material-notes'); facts.forEach((f) => ul.appendChild(el('li', '', f))); inner.append(el('div', 'eyebrow', 'BẢN SLICE, PHÒNG VÀ CUỘN NHỰA'), ul); }
    if (ins.material && Array.isArray(ins.material.notes) && ins.material.notes.length) {
      const notes = el('ul', 'material-notes');
      ins.material.notes.forEach((n) => notes.appendChild(el('li', '', n)));
      inner.append(el('div', 'eyebrow', `KINH NGHIỆM VỚI ${ins.material.type || 'NHỰA NÀY'}`), notes);
    }
    box.appendChild(inner);
    return box;
  }

  // Analysis assets need the auth header, so they load as blob URLs
  const assets = new Map(); // path -> Promise<string>
  function assetUrl(path) {
    if (!assets.has(path)) {
      const p = api(path).then((r) => r.blob()).then((b) => URL.createObjectURL(b))
        .catch((err) => { assets.delete(path); throw err; });
      assets.set(path, p);
    }
    return assets.get(path);
  }
  const viewers = new Map(); // analysis id -> viewer
  function disposeViewers(keep) {
    for (const [id, v] of viewers) {
      if (!keep || !keep.has(id)) { v.dispose(); viewers.delete(id); }
    }
  }

  function previewMessage(stage, text) {
    stage.textContent = '';
    stage.appendChild(el('p', 'preview-msg', text));
  }

  function showThumbnail(a, stage) {
    disposeViewers(new Set([...viewers.keys()].filter((id) => id !== a.id)));
    stage.dataset.mode = 'image';
    stage.textContent = '';
    const img = el('img', 'preview-img');
    img.alt = `Ảnh xem trước ${a.filename || 'model'}`;
    stage.appendChild(img);
    assetUrl(`/api/analyses/${encodeURIComponent(a.id)}/thumbnail`)
      .then((url) => { img.src = url; })
      .catch(() => previewMessage(stage, 'File không có ảnh xem trước. Bấm “Xem 3D” để xem model.'));
  }

  async function show3d(a, stage, button) {
    stage.dataset.mode = '3d';
    previewMessage(stage, 'Đang dựng bản xem 3D…');
    button.disabled = true;
    try {
      const res = await api(`/api/analyses/${encodeURIComponent(a.id)}/mesh`);
      const buf = await res.arrayBuffer();
      if (stage.dataset.mode !== '3d' || !stage.isConnected) return;
      stage.textContent = '';
      const canvas = el('canvas', 'preview-canvas');
      canvas.tabIndex = 0;
      canvas.setAttribute('aria-label', `Bản xem 3D của ${a.filename || 'model'}. Kéo để xoay, cuộn để phóng to, nhấp đúp để về góc ban đầu.`);
      stage.appendChild(canvas);
      if (viewers.has(a.id)) viewers.get(a.id).dispose();
      const v = window.PPViewer.create(canvas, buf);
      viewers.set(a.id, v);
      const hint = el('p', 'preview-hint', `Kéo để xoay · cuộn để phóng to · nhấp đúp để về góc ban đầu · ${v.triangles.toLocaleString('vi-VN')} tam giác · ${v.size.map((x) => x.toFixed(1)).join(' × ')} mm`);
      stage.appendChild(hint);
    } catch (err) {
      if (err instanceof AuthError) { handleError(err); return; }
      const msg = String(err && err.message || '');
      previewMessage(stage, msg.includes('404') ? 'File không còn trên máy chủ (đã quá hạn lưu).'
        : msg === 'webgl2' ? 'Trình duyệt này không hỗ trợ WebGL2 để xem 3D.'
        : 'Không dựng được bản xem 3D cho file này.');
    } finally {
      button.disabled = false;
    }
  }

  async function downloadProject(a, button, optimized = false) {
    button.disabled = true;
    try {
      const url = await assetUrl(`/api/analyses/${encodeURIComponent(a.id)}/${optimized ? 'optimized' : 'file'}`);
      const link = el('a');
      link.href = url;
      link.download = /\.3mf$/i.test(a.filename || '') ? a.filename : `${a.filename || 'model'}.3mf`;
      document.body.appendChild(link);
      link.click();
      link.remove();
    } catch (err) {
      if (err instanceof AuthError) handleError(err);
      else uploadStatus(optimized ? 'Không thể tạo 3MF tối ưu an toàn cho model này. Hãy kiểm tra cấu hình plate / object và thử lại.' : 'File không còn trên máy chủ (đã quá hạn lưu).', 'error');
    } finally {
      button.disabled = false;
    }
  }

  function buildPreview(a) {
    const wrap = el('div', 'analysis-preview');
    const stage = el('div', 'preview-stage');
    const actions = el('div', 'preview-actions');
    const bImg = el('button', 'btn');
    bImg.type = 'button';
    bImg.append(icon('image'), document.createTextNode('Ảnh'));
    const b3d = el('button', 'btn');
    b3d.type = 'button';
    b3d.append(icon('box'), document.createTextNode('Xem 3D'));
    const bDl = el('button', 'btn btn-quiet');
    bDl.type = 'button';
    bDl.append(icon('download'), document.createTextNode('Tải 3MF gốc'));
    const setMode = (mode) => {
      bImg.setAttribute('aria-pressed', String(mode === 'image'));
      b3d.setAttribute('aria-pressed', String(mode === '3d'));
    };
    bImg.addEventListener('click', () => { setMode('image'); showThumbnail(a, stage); });
    b3d.addEventListener('click', () => { setMode('3d'); show3d(a, stage, b3d); });
    bDl.addEventListener('click', () => downloadProject(a, bDl));
    const bOpt = el('button', 'btn btn-primary', 'Tải 3MF tối ưu');
    bOpt.type = 'button';
    bOpt.disabled = a.status === 'error' || !(a.changes || []).length;
    bOpt.addEventListener('click', () => downloadProject(a, bOpt, true));
    actions.append(bImg, b3d);
    const frame = el('div', 'preview-frame'); frame.append(stage, actions);
    const downloads = el('div', 'analysis-downloads'); if (bOpt.disabled) downloads.appendChild(el('p', 'muted', 'Không có đề xuất hợp lệ để ghi vào 3MF.')); downloads.append(bDl, bOpt);
    wrap.downloads = downloads;
    const goal = el('div', 'model-goal');
    goal.append(el('div', 'eyebrow', 'MỤC TIÊU TỐI ƯU'));
    const objectives = el('div', 'goal-options'); let objective = a.objective || 'quality';
    Object.entries(OBJECTIVE).forEach(([key, value]) => { const button = el('button', 'btn goal-tile'); button.append(icon({quality:'sparkles', strength:'layers', speed:'zap', economy:'gauge'}[key]), el('strong', '', value), el('small', '', {quality:'Bề mặt mịn, chi tiết rõ', strength:'Chắc chắn, chịu lực tốt', speed:'Rút ngắn thời gian in', economy:'Giảm vật liệu sử dụng'}[key])); button.type = 'button'; button.setAttribute('aria-pressed', String(key === objective)); button.addEventListener('click', () => { objective = key; objectives.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b === button))); }); objectives.appendChild(button); });
    const analyze = el('button', 'btn btn-primary analyze-again', 'Phân tích và tối ưu'); analyze.type = 'button';
    const result = el('p', 'muted'); result.setAttribute('role', 'status');
    analyze.addEventListener('click', async () => {
      analyze.disabled = true; result.textContent = 'Đang phân tích model với đánh giá mới nhất…';
      try { const response = await api(`/api/analyses/${encodeURIComponent(a.id)}/file`); const blob = await response.blob(); const project_base64 = await readBase64(blob); const {status, data} = await postJsonAllowError('/api/analyze', {filename: a.filename || 'model.3mf', project_base64, objective, trigger: 'manual', settings: {}}); if (status !== 200) throw new Error(data.error || `HTTP ${status}`); state.openAnalysis = data.id; await sync(); }
      catch (err) { result.textContent = `Không thể phân tích: ${err.message}`; if (err instanceof AuthError) handleError(err); }
      finally { analyze.disabled = false; }
    });
    goal.append(objectives, el('p', 'muted', 'AI dùng hình học, thông số 3MF và đánh giá của các lần in liên quan.'), analyze, result);
    const model = el('div', 'model-info'); model.append(el('h3', '', a.filename || 'Model của bạn'), el('p', 'muted', [a.material, a.printer_model, a.model_key ? `Model · ${a.model_key.slice(0,12)}` : 'Chưa xác định hình học'].filter(Boolean).join(' · ')));
    wrap.append(el('h3', 'model-title', 'Model của bạn'), frame, model);
    wrap.goal = goal;
    wrap.load = () => { if (!stage.dataset.mode) { setMode('image'); showThumbnail(a, stage); } };
    return wrap;
  }

  function analysesSignature(items) {
    return JSON.stringify(items.map((a) => [a.id, a.status, a.filename, a.summary, a.changes, a.warnings, a.feedback_context, a.insights ? 1 : 0]));
  }

  function renderAnalyses() {
    const items = (state.data.analyses || []).slice(0, 20);
    const root = $('analyses');
    $('analyses-empty').hidden = items.length > 0;
    $('nav-ai-count').hidden = !items.length;
    $('nav-ai-count').textContent = String((state.data.analyses || []).length);

    const metaText = (a) => [OBJECTIVE[a.objective] || a.objective, TRIGGER[a.trigger] || a.trigger, relTime(a.created_at)].filter(Boolean).join(' · ');
    // Unchanged list: refresh relative times only, keep open previews / 3D views
    const sig = analysesSignature(items);
    if (sig === state.analysesSig) {
      items.forEach((a) => {
        const meta = root.querySelector(`details[data-id="${CSS.escape(a.id)}"] .analysis-meta`);
        if (meta) meta.textContent = metaText(a);
      });
      return;
    }
    state.analysesSig = sig;
    const openIds = new Set([...root.querySelectorAll('details[open]')].map((d) => d.dataset.id));
    if (state.openAnalysis) { openIds.add(state.openAnalysis); }
    disposeViewers();
    root.textContent = '';

    items.forEach((a, index) => {
      // Only the newest analysis gets the model preview, goal picker and print
      // history; older ones show just the result and the optimized 3MF.
      const full = index === 0;
      const li = el('li');
      const det = el('details', 'analysis');
      det.dataset.id = a.id;
      det.open = openIds.has(a.id) || (!openIds.size && a === items[0]);
      const sum = el('summary');
      const ic = el('span', 'analysis-icon');
      ic.appendChild(icon(a.trigger === 'upload' ? 'upload' : 'wand-sparkles'));
      const left = el('div', 'analysis-head');
      left.appendChild(el('div', 'analysis-name', a.filename || 'Model'));
      const changes = Array.isArray(a.changes) ? a.changes : [];
      left.appendChild(el('div', 'analysis-meta', metaText(a)));
      const tag = a.status === 'error'
        ? el('span', 'tag tag-danger', 'Lỗi')
        : el('span', changes.length ? 'tag tag-ok' : 'tag', changes.length ? `${changes.length} thay đổi` : 'Giữ nguyên');
      sum.append(ic, left, tag, icon('chevron-down', 'analysis-chevron'));
      det.appendChild(sum);

      const body = el('div', full ? 'analysis-body' : 'analysis-body analysis-body-compact');
      const preview = full ? buildPreview(a) : null;
      const main = el('div', 'analysis-main');
      if (preview) main.appendChild(preview.goal);
      main.appendChild(el('div', 'eyebrow', 'KẾT QUẢ PHÂN TÍCH'));
      main.appendChild(el('h2', 'result-title', 'Thông số được đề xuất'));
      if (a.summary) { const callout = el('div', 'analysis-callout'); callout.append(el('span', 'ai-badge', 'AI'), el('p', a.status === 'error' ? 'analysis-summary analysis-error' : 'analysis-summary', a.summary)); main.appendChild(callout); }
      if (changes.length) {
        const table = el('table', 'changes');
        const head = el('tr');
        ['Thông số và lý do', 'Hiện tại', 'Đề xuất'].forEach((h) => head.appendChild(el('th', '', h)));
        table.appendChild(head);
        changes.forEach((c) => {
          if (!c || typeof c !== 'object') return;
          const tr = el('tr');
          const k = el('td');
          const label = SETTING_LABEL[c.key];
          k.appendChild(el('div', 'change-label', label ? label[0] : (c.key || '?')));
          if (label) k.appendChild(el('code', 'change-key', c.key));
          k.appendChild(el('small', 'change-reason', c.reason || ''));
          if (c.evidence) k.appendChild(el('small', 'change-evidence', `Căn cứ: ${c.evidence}`));
          const old = el('td', 'change-old num', c.old ?? '—'); old.dataset.label = 'Hiện tại';
          const v = el('td', 'change-value'); v.dataset.label = 'Đề xuất';
          v.appendChild(el('span', 'change-new num', c.value !== undefined ? String(c.value) : '?'));
          if (label && label[1]) v.appendChild(el('span', 'change-unit', ` ${label[1]}`));
          tr.append(k, old, v);
          table.appendChild(tr);
        });
        main.appendChild(table);
      }
      const warns = (Array.isArray(a.warnings) ? a.warnings : []).filter((w) => typeof w === 'string');
      if (warns.length) {
        const ul = el('ul', 'warn-list');
        warns.forEach((w) => ul.appendChild(el('li', '', w)));
        main.appendChild(ul);
      }
      const insightBox = buildInsight(a);
      if (insightBox) main.appendChild(insightBox);
      if (!full) {
        const bOpt = el('button', 'btn btn-primary', 'Tải 3MF tối ưu'); bOpt.type = 'button';
        bOpt.disabled = a.status === 'error' || !changes.length;
        bOpt.addEventListener('click', () => downloadProject(a, bOpt, true));
        const downloads = el('div', 'analysis-downloads'); if (bOpt.disabled) downloads.appendChild(el('p', 'muted', 'Không có đề xuất hợp lệ để ghi vào 3MF.')); downloads.appendChild(bOpt);
        main.appendChild(downloads); body.appendChild(main); det.appendChild(body); li.appendChild(det); root.appendChild(li);
        return;
      }
      const feedback = Array.isArray(a.feedback_context) ? a.feedback_context : [];
      const context = el('section', 'feedback-context');
      const contextHead = el('div', 'feedback-head'); contextHead.append(el('strong', '', 'Phản hồi AI đã tham khảo'), el('span', '', feedback.length ? `${feedback.length} lần in trước` : 'Chưa có'));
      context.appendChild(contextHead);
      if (!feedback.length) context.appendChild(el('p', 'muted', 'Chưa có đánh giá của lần in nào cùng model hoặc model có hình dạng tương tự. Đánh giá sau mỗi lần in sẽ được dùng ở lần phân tích tiếp theo.'));
      feedback.forEach((r) => { const review = r.review || r; const item = el('div', 'feedback-item'); const top = el('div', 'feedback-top'); top.append(el('b', '', [r.relation === 'similar_shape' ? `Model tương tự (${Math.round((r.similarity || 0) * 100)}%)` : '', r.job_name, r.printer_id, r.material].filter(Boolean).join(' · ') || 'Lần in'), el('span', '', [review.rating ? `${review.rating}/5` : '', r.started_at ? clock(r.started_at, true) : ''].filter(Boolean).join(' · '))); item.appendChild(top); if (review.comment) item.appendChild(el('p', '', `“${review.comment}”`)); if ((review.issues || []).length) item.appendChild(el('small', '', review.issues.join(' · '))); context.appendChild(item); });
      context.appendChild(el('p', 'feedback-note', 'Phản hồi là ngữ cảnh tham khảo; đề xuất vẫn được kiểm tra theo giới hạn máy và thông số hợp lệ.'));
      main.appendChild(context);
      main.appendChild(preview.downloads);
      body.append(preview, main);
      det.appendChild(body);
      const history = el('section', 'model-print-history');
      det.appendChild(history);
      let loaded = false;
      const loadHistory = () => { if (!loaded) { loaded = true; renderPrintHistory(a, history).catch((err) => { history.textContent = 'Không thể tải lịch sử in. Mở lại để thử lại.'; loaded = false; if (err instanceof AuthError) handleError(err); }); } };
      det.addEventListener('toggle', () => { if (det.open) loadHistory(); });
      if (det.open) loadHistory();
      det.addEventListener('toggle', () => {
        if (det.open) preview.load();
        else if (viewers.has(a.id)) { viewers.get(a.id).dispose(); viewers.delete(a.id); preview.querySelector('.preview-stage').dataset.mode = ''; preview.load(); }
      });
      if (det.open) preview.load();
      li.appendChild(det);
      root.appendChild(li);
    });
    if (state.openAnalysis) {
      const target = root.querySelector(`details[data-id="${CSS.escape(state.openAnalysis)}"]`);
      if (target) target.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      state.openAnalysis = null;
    }
  }

  // ---------------------------------------------------------------------------
  const PRINT_STATUS = {printing: 'Đang in', paused: 'Tạm dừng', completed: 'Hoàn thành', failed: 'Lỗi', cancelled: 'Đã hủy', interrupted: 'Bị gián đoạn', ended_unknown: 'Đã kết thúc'};
  const printerLabel = (id) => printerName((state.data.printers || []).find((p) => p.printer_id === id)) || id || '—';
  async function renderPrintHistory(a, root) {
    root.textContent = 'Đang tải lịch sử in…';
    const data = await (await api(`/api/analyses/${encodeURIComponent(a.id)}/prints`)).json();
    root.textContent = '';
    root.appendChild(el('h3', '', 'Lịch sử in của model'));
    const prints = data.prints || [];
    const linkPanel = el('details', 'feedback-context');
    linkPanel.appendChild(el('summary', '', 'Liên kết lần in chưa xác định model'));
    const linkBody = el('div', 'print-review'); linkPanel.appendChild(linkBody); root.appendChild(linkPanel);
    let fetched = false;
    linkPanel.addEventListener('toggle', async () => {
      if (!linkPanel.open || fetched) return; fetched = true; linkBody.textContent = 'Đang tải…';
      try {
        const response = await (await api('/api/prints')).json(); const unlinked = (response.prints || []).filter((p) => !p.analysis_id && !p.model_key);
        linkBody.textContent = ''; linkBody.appendChild(el('p', 'muted', 'Chỉ liên kết khi bạn xác nhận đúng model. Tên file không đủ để xác định hình học.'));
        if (!unlinked.length) linkBody.appendChild(el('p', 'muted', 'Không có lần in chưa liên kết.'));
        unlinked.forEach((print) => { const row = el('p'); row.appendChild(el('span', '', `${print.job_name || print.id} · ${print.printer_id ? printerLabel(print.printer_id) : '—'} · ${clock(print.started_at, true)} `)); const button = el('button', 'btn', 'Liên kết với model này'); button.type = 'button'; row.appendChild(button); linkBody.appendChild(row); button.addEventListener('click', async () => { button.disabled = true; try { await postJson(`/api/prints/${encodeURIComponent(print.id)}/link`, {analysis_id: a.id}); await renderPrintHistory(a, root); } catch (err) { button.disabled = false; row.appendChild(el('span', 'muted', ' Không thể liên kết. Hãy thử lại.')); if (err instanceof AuthError) handleError(err); } }); });
      } catch (err) { fetched = false; linkBody.textContent = 'Không thể tải lần in chưa liên kết.'; if (err instanceof AuthError) handleError(err); }
    });
    if (!prints.length) { root.appendChild(el('p', 'muted', 'Chưa có lần in được liên kết với model này.')); return; }
    const table = el('table', 'print-history-table');
    const head = el('tr');
    ['Lần in / Máy in', 'Trạng thái', 'Thời gian / Vật liệu', 'Đánh giá', ''].forEach((v) => head.appendChild(el('th', '', v)));
    table.appendChild(head);
    const editor = el('div', 'print-review');
    prints.forEach((print) => {
      const row = el('tr');
      const name = el('td');
      name.append(el('strong', '', print.job_name || print.id), el('small', '', `${print.printer_id ? printerLabel(print.printer_id) : '—'} · ${clock(print.started_at, true)}`));
      const action = el('td');
      const button = el('button', 'btn', !print.ended_at ? 'Chưa kết thúc' : print.review ? 'Xem đánh giá' : print.ai_review && !print.ai_review.error ? 'Xác nhận đánh giá AI' : 'Đánh giá');
      button.type = 'button'; button.disabled = !print.ended_at;
      button.addEventListener('click', () => {
        table.querySelectorAll('tr').forEach((r) => r.classList.toggle('selected-print', r === row));
        editor.textContent = '';
        editor.append(el('div', 'eyebrow', 'SAU MỖI LẦN IN'), el('h3', '', 'Bản in thực tế thế nào?'), el('p', 'muted review-sub', `${print.job_name || print.id} · Phản hồi giúp AI điều chỉnh đề xuất cho những lần in tương tự.`));
        const facts = el('dl', 'review-facts');
        [['Máy in', print.printer_id ? printerLabel(print.printer_id) : '—'], ['Vật liệu', print.material || 'Chưa rõ'], ['Kết quả / Thời lượng', [PRINT_STATUS[print.status] || print.status, print.duration_seconds == null ? '' : `${Math.round(print.duration_seconds / 60)} phút`].filter(Boolean).join(' · ')], ['Liên kết model', ({manager: 'Quản lý xác nhận', job_identity: 'Theo mã tác vụ', studio_send: 'Tự liên kết khi gửi in từ Studio'}[print.linkage_method] || 'Chưa xác định') + (print.linkage_method === 'studio_send' ? ` (độ tin cậy ${print.linkage_confidence === 'high' ? 'cao' : 'trung bình'})` : '')], ...(print.spool_usage && print.spool_usage.length ? [['Nhựa đã dùng', print.spool_usage.map((u) => `${u.grams} g${u.name ? ` từ «${u.name}» (còn ${u.remaining_g} g)` : u.note ? ` (${u.note})` : ''}`).join('; ')]] : []), ...(print.slice_stats ? [['Theo bản slice', `${Math.round((print.slice_stats.time_s || 0) / 60)} phút dự kiến · ${(print.slice_stats.filaments || []).map((f) => `${Math.round(f.weight_g)} g nhựa #${f.index}`).join(', ')}`]] : [])].forEach(([k, v]) => { const pair = el('div'); pair.append(el('dt', '', k), el('dd', '', v)); facts.appendChild(pair); });
        editor.appendChild(facts);
        editor.appendChild(el('p', 'muted review-facts-title', 'Thông số thực tế đã ghi nhận'));
        const settings = el('dl', 'review-settings');
        Object.entries(print.used_settings || print.settings || {}).forEach(([key, value]) => { const pair = el('div'); pair.append(el('dt', '', SETTING_LABEL[key]?.[0] || key), el('dd', '', typeof value === 'object' ? JSON.stringify(value) : value)); settings.appendChild(pair); });
        if (!settings.children.length) settings.appendChild(el('p', 'muted', 'Chưa có thông số thực tế được ghi nhận cho lần in này.'));
        editor.appendChild(settings);
        const ai = print.ai_review && !print.ai_review.error && print.ai_review.visible !== false ? print.ai_review : null;
        if (ai) {
          const hint = el('div', 'analysis-callout ai-review');
          hint.append(el('span', 'ai-badge', 'AI'), el('p', 'analysis-summary', `Ảnh lúc in xong: ${ai.summary}${ai.rating ? ` Gợi ý ${ai.rating}/5.` : ''}${print.review ? '' : ' Các ô dưới đã điền sẵn theo gợi ý, bạn sửa lại nếu khác thực tế rồi gửi.'}`));
          editor.appendChild(hint);
        }
        const draft = print.review || (ai ? { rating: ai.rating, issues: ai.issues || [] } : null);
        const form = el('form');
        const ratings = el('fieldset', 'rating-options');
        ratings.appendChild(el('legend', '', 'Chất lượng tổng thể'));
        for (let i = 1; i <= 5; i++) {
          const label = el('label'); const input = el('input');
          input.type = 'radio'; input.name = 'rating'; input.value = String(i); input.required = true; input.checked = draft?.rating === i;
          const face = el('span', 'rating-face'); face.append(el('b', '', String(i)), el('small', '', ['Rất tệ', 'Chưa tốt', 'Tạm ổn', 'Tốt', 'Rất tốt'][i - 1])); label.append(input, face); ratings.appendChild(label);
        }
        const issues = el('fieldset', 'issue-options'); issues.appendChild(el('legend', '', 'Vấn đề quan sát được'));
        const choices = [...new Set(['Bám bàn kém', 'Tơ nhựa', 'Lệch lớp', 'Thiếu nhựa', 'Bề mặt xấu', 'Cong vênh', 'Phần treo xấu', 'Nứt lớp', 'Bọt/ẩm', ...(draft?.issues || [])])];
        choices.forEach((value) => { const label = el('label'); const input = el('input'); input.type = 'checkbox'; input.name = 'issues'; input.value = value; input.checked = (draft?.issues || []).includes(value); label.append(input, document.createTextNode(value)); issues.appendChild(label); });
        const label = el('label', 'field', 'Nhận xét thêm'); const comment = el('textarea'); comment.name = 'comment'; comment.maxLength = 1500; comment.rows = 3; comment.value = print.review?.comment || ''; label.appendChild(comment);
        const submit = el('button', 'btn btn-primary review-submit', print.review ? 'Cập nhật đánh giá' : 'Gửi đánh giá'); submit.type = 'submit';
        const result = el('p', 'muted'); result.setAttribute('role', 'status');
        form.append(ratings, issues, label, submit, result);
        form.addEventListener('submit', async (event) => {
          event.preventDefault(); if (!form.reportValidity()) return; submit.disabled = true; result.textContent = 'Đang lưu…';
          try { await postJson(`/api/prints/${encodeURIComponent(print.id)}/review`, {rating: Number(form.elements.rating.value), issues: [...issues.querySelectorAll('input:checked')].map((i) => i.value), comment: comment.value}); await renderPrintHistory(a, root); }
          catch (err) { result.textContent = 'Không thể lưu đánh giá. Vui lòng thử lại.'; if (err instanceof AuthError) handleError(err); }
          finally { submit.disabled = false; }
        });
        const layout = el('div', 'review-layout'); const context = el('aside', 'review-context'); while (editor.children.length > 3) context.appendChild(editor.children[3]); layout.append(context, form); editor.appendChild(layout);
      });
      action.appendChild(button); const cells = [el('td', `print-status print-status-${print.status || 'unknown'}`, PRINT_STATUS[print.status] || print.status || '—'), el('td', '', [print.duration_seconds == null ? '—' : `${Math.round(print.duration_seconds / 60)} phút`, print.material || 'Chưa rõ vật liệu'].join(' · ')), el('td', 'print-rating', print.review ? `${print.review.rating}/5${(print.review.issues || []).length ? ` · ${print.review.issues.join(', ')}` : ''}` : 'Chưa đánh giá')];
      ['Kết quả', 'Thời gian / Vật liệu', 'Đánh giá'].forEach((label, i) => { cells[i].dataset.label = label; });
      action.className = 'print-action'; row.append(name, ...cells, action); table.appendChild(row);
    });
    const scroll = el('div', 'table-scroll'); scroll.appendChild(table); root.append(scroll, linkPanel, editor); editor.hidden = true;
    table.addEventListener('click', () => { editor.hidden = false; });
    const ended = prints.filter((p) => p.ended_at);
    const initial = ended.find((p) => !p.review) || ended[0];
    if (initial) { const idx = prints.indexOf(initial); table.querySelectorAll('tr')[idx + 1]?.querySelector('button')?.click(); }
  }

  const filamentProfiles = new Map();
  let selectedFilament = '';
  function renderFilament(p) {
    const root = $('filament-panel');
    const ams = p.telemetry?.ams;
    const units = Array.isArray(ams?.units) ? ams.units : [];
    const signature = JSON.stringify([p.printer_id, ams, p.stale, p.status, p.ams_edit_supported]);
    if (root.dataset.signature === signature) return;
    root.dataset.signature = signature;
    root.textContent = '';
    root.appendChild(el('h3', '', `Filament${units.length ? ` · ${units.length} bộ AMS` : ''}`));
    if (!ams) { root.appendChild(el('p', 'muted', 'Chưa có dữ liệu filament từ máy in.')); return; }
    const all = [];
    const addUnit = (unit, external = false) => {
      const row = el('div', 'ams-row'); const name = el('div', 'ams-name');
      name.append(el('strong', '', unit.name || `AMS ${unit.id}`), el('small', '', external ? 'Không qua AMS' : `Độ ẩm: ${unit.humidity ?? '—'}`));
      const slots = el('div', 'ams-slots');
      (unit.slots || []).forEach((slot) => {
        const key = `${p.printer_id}/${unit.id}/${slot.id}`;
        all.push({unit, slot, key});
        const button = el('button', `filament-chip${slot.active ? ' in-use' : ''}`); button.type = 'button';
        button.dataset.slotKey = key;
        button.setAttribute('aria-pressed', String(selectedFilament === key));
        const dot = el('span', 'filament-dot'); dot.style.setProperty('--filament', /^[0-9a-f]{6,8}$/i.test(slot.color || '') ? `#${slot.color.slice(0,6)}` : 'transparent');
        const text = el('span', 'chip-label'); text.append(el('b', '', `${external ? 'Ngoài' : (slot.label || slot.id)}${slot.present && slot.filament_name && slot.material ? ` · ${slot.material}` : ''}${slot.rfid === true ? ' · RFID' : ''}`), el('span', '', slot.present ? slot.filament_name || slot.material || 'Chưa rõ vật liệu' : 'Trống')); if (slot.present && slot.filament_name && slot.material) button.title = `${slot.filament_name} · ${slot.material}`;
        button.append(dot, text); button.addEventListener('click', () => { selectedFilament = key; delete root.dataset.signature; renderFilament(p); }); slots.appendChild(button);
      });
      row.append(name, slots); root.appendChild(row);
    };
    units.forEach((unit) => addUnit(unit));
    if (ams.external) addUnit({id: 255, name: 'Cuộn ngoài', slots: [ams.external]}, true);
    if (!all.length) { root.appendChild(el('p', 'muted', 'Không có khe filament được báo cáo.')); return; }
    const chosen = all.find((x) => x.key === selectedFilament) || all.find((x) => x.slot.active) || all[0];
    selectedFilament = chosen.key;
    root.querySelectorAll('.filament-chip').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.slotKey === selectedFilament)));
    const {unit, slot} = chosen;
    const detail = el('div', 'slot-detail'); detail.append(el('strong', '', `${unit.id === 255 ? 'Cuộn ngoài' : `Khe ${slot.label || slot.id}`} · ${slot.present ? (slot.filament_name || slot.material || 'Chưa rõ vật liệu') : 'Trống'}`), el('span', '', `Còn lại: ${slot.remaining == null || slot.remaining < 0 ? 'Chưa có dữ liệu' : `${slot.remaining}%`}`)); root.appendChild(detail);
    if (slot.present) {
      const facts = el('dl', 'slot-facts');
      const fact = (k, v, hint) => { const d = el('div'); d.append(el('dt', '', k), el('dd', '', v)); if (hint) d.appendChild(el('small', '', hint)); facts.appendChild(d); };
      fact('Loại nhựa', slot.material || 'Chưa rõ');
      fact('Màu', /^[0-9a-f]{6,8}$/i.test(slot.color || '') ? `#${slot.color.slice(0, 6).toUpperCase()}` : 'Chưa rõ');
      const k = typeof slot.flow_k === 'number' ? slot.flow_k.toFixed(3) : null;
      const calibrated = Number.isInteger(slot.cali_idx) && slot.cali_idx >= 0;
      fact('Dynamic flow (hệ số K)', k ? `K ${k}` : 'Chưa có dữ liệu', calibrated ? `Đang dùng profile hiệu chuẩn #${slot.cali_idx} trên máy` : (k ? 'Mặc định của vật liệu (chưa chọn profile hiệu chuẩn)' : ''));
      root.appendChild(facts);
    }
    const firmwareBlocked = p.ams_edit_supported === false;
    const locked = firmwareBlocked || slot.rfid !== false || slot.present !== true || printerStatus(p) === 'offline' || (['printing', 'paused'].includes(printerStatus(p)) && ams.supports_edit_while_printing !== true);
    const notice = el('p', 'filament-note', firmwareBlocked ? 'Firmware máy in này không hỗ trợ chỉnh filament từ PrintPilot (máy từ chối với mã 0502-4007). Hãy đổi trên màn hình máy in hoặc Bambu Studio.' : slot.rfid === true ? 'RFID · Chỉ đọc. Vật liệu và màu được nhận diện tự động.' : locked ? 'Không thể chỉnh khe: thiếu dữ liệu quyền ghi, khe trống hoặc máy chưa hỗ trợ chỉnh trong trạng thái hiện tại.' : 'Chỉnh thông tin khai báo của cuộn đang lắp. Không thay đổi thông số tác vụ đã cắt lớp.');
    // Firmware that rejects material edits: show the slot read-only, without edit controls.
    if (firmwareBlocked) return;
    root.appendChild(notice);
    const edit = el('button', 'btn', locked ? 'Chỉ đọc' : `Chỉnh sửa ${unit.id === 255 ? 'cuộn ngoài' : `khe ${slot.label || slot.id}`}`); edit.type = 'button'; edit.disabled = locked; root.appendChild(edit);
    edit.addEventListener('click', async () => {
      edit.disabled = true;
      try {
        if (!filamentProfiles.has(p.printer_id)) { const data = await (await api(`/api/filament-profiles?printer_id=${encodeURIComponent(p.printer_id)}`)).json(); filamentProfiles.set(p.printer_id, data.profiles || []); }
        const profiles = filamentProfiles.get(p.printer_id);
        if (!profiles.length) { notice.textContent = 'Máy chủ chưa có profile tương thích. Không thể lưu.'; return; }
        const form = el('form', 'ams-editor'); const fields = el('fieldset');
        const label = el('label', 'field', 'Profile vật liệu'); const select = el('select'); select.required = true;
        profiles.forEach((profile) => { const option = el('option', '', profile.name); option.value = profile.id; option.selected = profile.filament_id === slot.filament_id && profile.setting_id === slot.setting_id; select.appendChild(option); }); label.appendChild(select);
        const colorLabel = el('div', 'field color-field'); colorLabel.appendChild(el('span', 'field-label', 'Màu filament'));
        const colorRow = el('div', 'color-row'); const color = el('input'); color.type = 'color'; color.className = 'color-swatch'; color.setAttribute('aria-label', 'Chọn màu filament'); color.value = /^[0-9a-f]{6,8}$/i.test(slot.color || '') ? `#${slot.color.slice(0,6).toLowerCase()}` : '#ffffff';
        const hex = el('input', 'color-hex'); hex.type = 'text'; hex.maxLength = 7; hex.value = color.value.toUpperCase(); hex.setAttribute('aria-label', 'Mã màu HEX'); hex.pattern = '#[0-9A-Fa-f]{6}';
        color.addEventListener('input', () => { hex.value = color.value.toUpperCase(); presets.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.color === color.value))); });
        hex.addEventListener('input', () => { if (/^#[0-9a-f]{6}$/i.test(hex.value)) { color.value = hex.value.toLowerCase(); color.dispatchEvent(new Event('input')); } });
        const presets = el('div', 'color-presets');
        ['#ffffff', '#000000', '#a4aaac', '#c52c18', '#ff6910', '#f4d03f', '#18c241', '#1f6feb', '#8e44ad', '#f5a3c7'].forEach((c) => { const b = el('button', 'color-preset'); b.type = 'button'; b.dataset.color = c; b.style.setProperty('--swatch', c); b.setAttribute('aria-label', `Màu ${c.toUpperCase()}`); b.setAttribute('aria-pressed', String(c === color.value)); b.addEventListener('click', () => { color.value = c; color.dispatchEvent(new Event('input')); }); presets.appendChild(b); });
        colorRow.append(color, hex); colorLabel.append(colorRow, presets);
        const submit = el('button', 'btn btn-primary', 'Lưu vào máy in'); submit.type = 'submit'; const cancel = el('button', 'btn', 'Hủy'); cancel.type = 'button'; cancel.onclick = () => { form.remove(); edit.disabled = false; };
        const result = el('p', 'filament-note'); result.setAttribute('role', 'status'); fields.append(label, colorLabel); const actions = el('div', 'ams-editor-actions'); actions.append(result, cancel, submit); form.append(fields, actions); root.appendChild(form);
        form.addEventListener('submit', async (event) => {
          event.preventDefault(); if (!form.reportValidity()) return; fields.disabled = true; submit.disabled = true;
          result.textContent = 'Đang gửi lệnh…';
          try { const reply = await postJsonAllowError(`/api/printers/${encodeURIComponent(p.printer_id)}/commands`, {command: 'ams_filament_setting', params: {ams_id: unit.id, slot_id: slot.id, profile_id: select.value, color: color.value}}); if (reply.status >= 400) throw new Error(reply.data.error || 'Lệnh bị từ chối'); result.textContent = 'Đã gửi lệnh. Chờ máy in xác nhận; chưa đồng bộ.';
            if (!reply.data.id) throw new Error('Máy chủ không trả mã lệnh');
            const deadline = Date.now() + 45000;
            while (Date.now() < deadline) {
              await new Promise((resolve) => setTimeout(resolve, 1500));
              const command = await (await api(`/api/commands/${encodeURIComponent(reply.data.id)}`)).json();
              if (['failed', 'expired'].includes(command.status)) throw new Error(command.result || 'Máy in không xác nhận lệnh');
              if (command.status === 'done') {
                await sync();
                const fresh = (state.data.printers || []).find((x) => x.printer_id === p.printer_id);
                const updated = unit.id === 255 ? fresh?.telemetry?.ams?.external : fresh?.telemetry?.ams?.units?.find((u) => u.id === unit.id)?.slots?.find((t) => t.id === slot.id);
                const profile = profiles.find((x) => x.id === select.value);
                if (updated && updated.color?.slice(0,6).toLowerCase() === color.value.slice(1).toLowerCase() && updated.filament_id === profile.filament_id && updated.setting_id === profile.setting_id) { notice.textContent = 'Đã đồng bộ · Máy in xác nhận và dữ liệu filament đã cập nhật.'; form.remove(); edit.disabled = false; return; }
                result.textContent = 'Máy in đã xác nhận. Chờ telemetry khớp thay đổi; chưa đồng bộ.';
              }
            }
            throw new Error('Hết thời gian chờ dữ liệu xác nhận. Hãy tải lại và kiểm tra khe trước khi thử lại.'); }
          catch (err) { result.textContent = `Không thể lưu: ${err.message}`; fields.disabled = false; submit.disabled = false; if (err instanceof AuthError) handleError(err); }
        });
      } catch (err) { notice.textContent = 'Không thể tải profile vật liệu. Hãy thử lại.'; edit.disabled = false; if (err instanceof AuthError) handleError(err); }
    });
  }

  // Upload a 3MF for analysis
  // ---------------------------------------------------------------------------
  const MAX_UPLOAD = 64 * 1024 * 1024;
  function uploadStatus(text, kind = '') {
    const node = $('upload-status');
    node.textContent = text;
    node.dataset.state = kind;
    node.hidden = !text;
  }
  function readBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ''));
      reader.onerror = () => reject(reader.error || new Error('read'));
      reader.readAsDataURL(file);
    });
  }
  async function uploadProject(file) {
    if (!file) return;
    if (!/\.3mf$/i.test(file.name)) { uploadStatus('Chỉ nhận file .3mf (dự án Bambu Studio).', 'error'); return; }
    if (file.size > MAX_UPLOAD) { uploadStatus(`File quá lớn (${formatBytes(file.size)}). Giới hạn là 64 MB.`, 'error'); return; }
    const btn = $('upload-btn');
    btn.disabled = true;
    uploadStatus(`Đang tải lên và phân tích “${file.name}”… AI có thể mất tới một phút.`, 'busy');
    try {
      const project_base64 = await readBase64(file);
      const { status, data } = await postJsonAllowError('/api/analyze', {
        filename: file.name, project_base64, objective: $('upload-objective').value, trigger: 'upload', settings: {},
      });
      if (status !== 200) {
        uploadStatus(status === 422 ? 'Không đọc được file 3MF này.' : (data.error || `Tải lên thất bại (HTTP ${status}).`), 'error');
        return;
      }
      state.openAnalysis = data.id;
      if (data.status === 'error') uploadStatus(`Đã lưu file nhưng AI phân tích lỗi: ${data.summary || 'không rõ lý do'}`, 'error');
      else uploadStatus(data.changes && data.changes.length ? `Xong: AI đề xuất ${data.changes.length} thay đổi.` : 'Xong: AI thấy thông số hiện tại đã phù hợp.', 'ok');
      await sync();
    } catch (err) {
      if (err instanceof AuthError) handleError(err);
      else uploadStatus('Không tải lên được. Kiểm tra kết nối tới máy chủ rồi thử lại.', 'error');
    } finally {
      btn.disabled = false;
      $('upload-input').value = '';
    }
  }
  function bindUpload() {
    const input = $('upload-input');
    $('upload-btn').addEventListener('click', () => input.click());
    input.addEventListener('change', () => uploadProject(input.files && input.files[0]));
    const view = document.querySelector('[data-view="ai"]');
    view.addEventListener('dragover', (e) => {
      if (![...e.dataTransfer.types].includes('Files')) return;
      e.preventDefault();
      view.classList.add('drop-target');
    });
    view.addEventListener('dragleave', (e) => { if (!view.contains(e.relatedTarget)) view.classList.remove('drop-target'); });
    view.addEventListener('drop', (e) => {
      e.preventDefault();
      view.classList.remove('drop-target');
      if (!$('upload-btn').disabled) uploadProject(e.dataTransfer.files && e.dataTransfer.files[0]);
    });
  }

  // ---------------------------------------------------------------------------
  // Detail dialog
  // ---------------------------------------------------------------------------
  function openDetail(printerId, live = false) {
    if (state.detailPrinterId !== printerId && state.live) stopLive();
    state.detailPrinterId = printerId;
    state.detailSnapshotId = null;
    const dlg = $('detail');
    const docked = WIDE.matches;
    if (dlg.open && dlg.classList.contains('docked') !== docked) closeDetailKeepSelection();
    dlg.classList.toggle('docked', docked);
    renderDetail();
    if (!dlg.open) {
      if (docked) {
        dlg.show();
        // show() focuses the first control; keep focus on the list instead
        if (dlg.contains(document.activeElement)) document.activeElement.blur();
      } else {
        dlg.showModal();
      }
    }
    markSelected();
    if (live) startLive(printerId);
  }

  // --- Rename (manager-chosen display name; empty restores the bridge-reported name) ---
  function renameEditing(on) {
    $('rename-form').hidden = !on;
    $('detail-title').closest('.detail-title-row').hidden = on;
    $('rename-error').hidden = true;
  }
  function startRename() {
    const p = (state.data.printers || []).find((x) => x.printer_id === state.detailPrinterId);
    if (!p) return;
    const input = $('rename-input');
    input.value = p.custom_name || p.name || '';
    input.placeholder = p.reported_name || p.printer_id;
    renameEditing(true);
    input.focus();
    input.select();
  }
  async function submitRename(e) {
    e.preventDefault();
    const id = state.detailPrinterId;
    const p = (state.data.printers || []).find((x) => x.printer_id === id);
    if (!p) return;
    const form = $('rename-form');
    const err = $('rename-error');
    let name = $('rename-input').value.trim().replace(/\s+/g, ' ');
    if (name === (p.reported_name || '')) name = ''; // same as the bridge name: just follow it
    form.setAttribute('aria-busy', 'true');
    try {
      const { status, data } = await postJsonAllowError(`/api/printers/${encodeURIComponent(id)}/name`, { name });
      if (status !== 200) throw new Error(data.error || 'Không đổi được tên');
      Object.assign(p, { name: data.name, custom_name: data.custom_name, reported_name: data.reported_name });
      renameEditing(false);
      renderPrinters();
      renderDetail();
      syncEventPrinterOptions();
      sync();
    } catch (ex) {
      if (ex instanceof AuthError) { handleError(ex); return; }
      err.textContent = ex.message || 'Không đổi được tên';
      err.hidden = false;
    } finally {
      form.removeAttribute('aria-busy');
    }
  }

  function closeDetailKeepSelection() {
    const id = state.detailPrinterId;
    $('detail').close();          // 'close' handler stops live view and clears the selection
    state.detailPrinterId = id;
  }

  function onLayoutChange() {
    const dlg = $('detail');
    if (WIDE.matches) {
      const id = state.detailPrinterId || ((state.data.printers || [])[0] || {}).printer_id;
      if (id) openDetail(id);
    } else if (dlg.open && dlg.classList.contains('docked')) {
      dlg.close();
    }
    markSelected();
  }

  // ---------------------------------------------------------------------------
  // Live camera (frames relayed by the server from the LAN bridge)
  // ---------------------------------------------------------------------------
  function liveCaption(text, kind = '') {
    const c = $('live-state');
    c.textContent = text;
    c.dataset.state = kind;
    c.hidden = !text;
  }

  function renderLiveButton() {
    const btn = $('btn-live');
    const on = Boolean(state.live && state.live.printerId === state.detailPrinterId);
    btn.setAttribute('aria-label', on ? 'Dừng xem' : 'Xem trực tiếp');
    btn.setAttribute('aria-pressed', String(on));
    btn.querySelector('.live-toggle-label').textContent = on ? 'Dừng' : 'Trực tiếp';
    btn.querySelector('[data-live="off"]').toggleAttribute('hidden', on);
    btn.querySelector('[data-live="on"]').toggleAttribute('hidden', !on);
  }

  async function startLive(printerId) {
    stopLive(false);
    const token = {};
    state.live = { printerId, seq: 0, token, url: null, frames: [] };
    renderLiveButton();
    liveCaption('Đang kết nối camera…');
    const L = state.live;
    const renew = async () => {
      const { status, data } = await postJsonAllowError(`/api/printers/${encodeURIComponent(printerId)}/live`, {});
      if (status !== 200) throw new Error(data.error || 'Không bật được xem trực tiếp');
    };
    try {
      await renew();
      let lastRenew = Date.now();
      let waitingSince = Date.now();
      while (state.live && state.live.token === token) {
        if (Date.now() - lastRenew > 8000) { await renew(); lastRenew = Date.now(); }
        const res = await api(`/api/printers/${encodeURIComponent(printerId)}/live/frame?after=${L.seq}`);
        if (!state.live || state.live.token !== token) break;
        if (res.status === 204) {
          if (!L.seq && Date.now() - waitingSince > 25000) throw new Error('Cầu nối chưa gửi hình. Kiểm tra Redmi.');
          continue;
        }
        const blob = await res.blob();
        L.seq = Number(res.headers.get('X-Frame-Seq')) || L.seq + 1;
        const url = URL.createObjectURL(blob);
        const img = $('detail-img');
        img.dataset.id = 'live';
        img.src = url;
        if (L.url) URL.revokeObjectURL(L.url);
        L.url = url;
        const now = Date.now();
        L.frames = L.frames.filter((t) => now - t < 15000).concat(now);
        const fps = L.frames.length > 1 ? (L.frames.length - 1) / ((now - L.frames[0]) / 1000) : 0;
        liveCaption(`TRỰC TIẾP · ${fps ? fps.toFixed(1) : '…'} fps · ${clock(new Date().toISOString())}`, 'live');
        waitingSince = now;
      }
    } catch (err) {
      if (err instanceof AuthError) { handleError(err); return; }
      if (state.live && state.live.token === token) {
        liveCaption(err.message || 'Mất kết nối xem trực tiếp', 'error');
        stopLive(true, true);
      }
    }
  }

  function stopLive(tellServer = true, keepCaption = false) {
    const L = state.live;
    if (!L) return;
    state.live = null;
    if (tellServer) postJsonAllowError(`/api/printers/${encodeURIComponent(L.printerId)}/live`, { action: 'stop' }).catch(() => {});
    if (L.url) setTimeout(() => URL.revokeObjectURL(L.url), 1000);
    const img = $('detail-img');
    if (img.dataset.id === 'live') delete img.dataset.id;   // let renderDetail restore the snapshot
    if (!keepCaption) liveCaption('');
    renderLiveButton();
    if ($('detail').open) renderDetail();
  }

  // ---------------------------------------------------------------------------
  // Printer controls
  // ---------------------------------------------------------------------------
  const SPEED = { 1: 'Êm', 2: 'Chuẩn', 3: 'Thể thao', 4: 'Siêu tốc' };
  const CMD_LABEL = { pause: 'Tạm dừng', resume: 'Tiếp tục', stop: 'Dừng in' };

  // Feedback for one printer, shown on its card and in the detail panel
  function cmdStatus(printerId, text, kind = '') {
    if (text) state.cmdMsg[printerId] = { text, kind, at: Date.now() };
    else delete state.cmdMsg[printerId];
    if (state.detailPrinterId === printerId) ctlStatus(text, kind);
    const card = document.querySelector(`#printers .printer[data-printer="${CSS.escape(printerId)}"]`);
    if (card) renderCardMsg(card, printerId);
  }
  function ctlStatus(text, kind = '') {
    const n = $('ctl-status');
    n.textContent = text;
    n.dataset.state = kind;
  }
  function renderCardMsg(card, printerId) {
    const m = state.cmdMsg[printerId];
    const n = card.querySelector('.card-msg');
    // Success fades after a few seconds; errors stay a little longer
    const fresh = m && (Date.now() - m.at) < (m.kind === 'error' ? 20000 : m.kind === 'ok' ? 6000 : 60000);
    n.textContent = fresh ? m.text : '';
    n.dataset.state = fresh ? m.kind : '';
    clearTimeout(card.msgTimer);
    if (fresh && m.kind) card.msgTimer = setTimeout(() => renderCardMsg(card, printerId), 6100);
  }

  // Enable/label every [data-cmd] button under root for printer p
  function applyControls(root, p) {
    const status = printerStatus(p);
    const t = p.telemetry || {};
    const busy = Boolean(state.cmds[p.printer_id]);
    const allowed = {
      pause: status === 'printing',
      resume: status === 'paused',
      stop: ['printing', 'paused', 'error'].includes(status),
    };
    root.querySelectorAll('[data-toggle="pause"]').forEach((b) => {
      const resume = status === 'paused';
      b.dataset.cmd = resume ? 'resume' : 'pause';
      b.setAttribute('aria-label', resume ? 'Tiếp tục' : 'Tạm dừng');
      b.dataset.tip = resume ? 'Tiếp tục' : 'Tạm dừng';
      b.querySelector('[data-icon="pause"]').toggleAttribute('hidden', resume);
      b.querySelector('[data-icon="play"]').toggleAttribute('hidden', !resume);
    });
    root.querySelectorAll('[data-cmd]').forEach((b) => {
      const cmd = b.dataset.cmd;
      let enabled = status !== 'offline' && !busy;
      if (cmd in allowed) enabled = enabled && allowed[cmd];
      if (cmd === 'speed') {
        enabled = enabled && (status === 'printing' || status === 'paused');
        b.setAttribute('aria-pressed', String(Number(b.dataset.level) === t.speed_level));
      }
      b.disabled = !enabled;
    });
    const lit = t.light === 'on';
    root.querySelectorAll('[data-cmd="light"]').forEach((light) => {
      light.dataset.mode = lit ? 'off' : 'on';
      light.setAttribute('aria-label', lit ? 'Tắt đèn' : 'Bật đèn');
      light.dataset.tip = lit ? 'Tắt đèn' : 'Bật đèn';
      light.setAttribute('aria-pressed', String(lit));
      light.querySelector('[data-light="on"]').toggleAttribute('hidden', lit);
      light.querySelector('[data-light="off"]').toggleAttribute('hidden', !lit);
    });
  }

  function renderControls(p) {
    applyControls($('detail'), p);
    const t = p.telemetry || {};
    $('speed-current').textContent = SPEED[t.speed_level] || '--';
    const m = state.cmdMsg[p.printer_id];
    if (state.cmds[p.printer_id] || (m && Date.now() - m.at < 20000)) ctlStatus(m ? m.text : '', m ? m.kind : '');
    else if (printerStatus(p) === 'offline') ctlStatus('Máy in mất kết nối, không thể điều khiển.', 'error');
    else ctlStatus('');
  }

  function refreshControls(printerId) {
    const p = (state.data.printers || []).find((x) => x.printer_id === printerId);
    if (!p) return;
    const card = document.querySelector(`#printers .printer[data-printer="${CSS.escape(printerId)}"]`);
    if (card) applyControls(card, p);
    if (state.detailPrinterId === printerId) renderControls(p);
  }

  async function runCommand(button, printerId) {
    const p = (state.data.printers || []).find((x) => x.printer_id === printerId);
    if (!p || state.cmds[p.printer_id]) return;
    const pid = p.printer_id;
    const command = button.dataset.cmd;
    const params = {};
    let label = CMD_LABEL[command] || command;
    if (command === 'light') { params.mode = button.dataset.mode; label = params.mode === 'on' ? 'Bật đèn' : 'Tắt đèn'; }
    if (command === 'speed') { params.level = Number(button.dataset.level); label = `Tốc độ ${SPEED[params.level]}`; }
    if (command === 'stop' && !window.confirm(`Dừng hẳn bản in trên ${printerName(p)}? Không thể tiếp tục sau khi dừng.`)) return;

    state.cmds[pid] = label;
    refreshControls(pid);
    cmdStatus(pid, `${label}: đang gửi…`);
    try {
      const { status, data } = await postJsonAllowError(`/api/printers/${encodeURIComponent(pid)}/commands`, { command, params });
      if (status !== 202) { cmdStatus(pid, data.error || 'Không gửi được lệnh', 'error'); return; }
      const deadline = Date.now() + 45000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 1500));
        const st = await (await api(`/api/commands/${encodeURIComponent(data.id)}`)).json();
        if (st.status === 'sent') cmdStatus(pid, `${label}: máy in đang xử lý…`);
        if (st.status === 'done') {
          cmdStatus(pid, `${label}: máy in đã thực hiện.`, 'ok');
          // Show the confirmed state now; an older bridge may report it up to 30 s later
          if (command === 'light') state.expect[pid] = { key: 'light', value: params.mode, until: Date.now() + 40000 };
          if (command === 'speed') state.expect[pid] = { key: 'speed_level', value: params.level, until: Date.now() + 40000 };
          applyExpectations();
          sync();
          return;
        }
        if (st.status === 'failed') { cmdStatus(pid, `${label}: máy in từ chối (${st.result || 'không rõ'}).`, 'error'); return; }
        if (st.status === 'expired') { cmdStatus(pid, `${label}: cầu nối không nhận lệnh. Kiểm tra Redmi.`, 'error'); return; }
      }
      cmdStatus(pid, `${label}: quá thời gian chờ phản hồi.`, 'error');
    } catch (err) {
      if (err instanceof AuthError) handleError(err);
      else cmdStatus(pid, 'Lỗi kết nối máy chủ', 'error');
    } finally {
      delete state.cmds[pid];
      refreshControls(pid);
    }
  }

  function applyExpectations() {
    const now = Date.now();
    (state.data.printers || []).forEach((p) => {
      const e = state.expect[p.printer_id];
      if (!e) return;
      p.telemetry = p.telemetry || {};
      if (now > e.until || p.telemetry[e.key] === e.value) { delete state.expect[p.printer_id]; return; }
      p.telemetry[e.key] = e.value;
    });
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
    $('detail-title').title = p.custom_name && p.reported_name ? `Tên gốc: ${p.reported_name}` : '';
    $('detail-sub').textContent = [p.model, t.job_name].filter(Boolean).join(' · ');
    const pill = $('detail-status');
    pill.dataset.status = status;
    pill.textContent = '';
    pill.append(el('span', 'status-dot'), document.createTextNode(STATUS[status]));

    const img = $('detail-img');
    const watching = state.live && state.live.printerId === p.printer_id;
    renderLiveButton();
    $('btn-live').disabled = status === 'offline' && !watching;
    if (watching) {
      // live frames own the image while streaming
    } else if (current) {
      const at = current.captured_at || current.created_at;
      img.alt = `Ảnh camera lúc ${clock(at, true)}`;
      if (img.dataset.id !== current.id) setImage(img, current.id);
      $('detail-caption').textContent = `${clock(at, true)} · ${relTime(at)}`;
    } else {
      img.removeAttribute('src');
      delete img.dataset.id;
      $('detail-caption').textContent = '';
    }

    let dv = vision(current);
    if ((status === 'idle' || status === 'offline') && current === snaps[0] && current
        && ageSec(current.captured_at || current.created_at) > STALE_IMAGE_SEC) {
      dv = { state: 'none', icon: 'rest', text: 'Máy không in nên camera tạm nghỉ, ảnh tự cập nhật khi bắt đầu in.' };
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
      ['Tiến độ', typeof tv.progress === 'number' ? `${tv.progress}%` : '--'],
      ['Thời gian còn lại', status === 'printing' && tv.remaining_min !== undefined ? minutes(tv.remaining_min).replace('còn ', '') : '--'],
      ['Lớp', tv.current_layer !== undefined ? `${tv.current_layer} / ${tv.total_layers ?? '?'}` : '--'],
      ['Đầu in', tempPair(tv.nozzle_temp, tv.nozzle_target)],
      ['Bàn in', tempPair(tv.bed_temp, tv.bed_target)],
      ['Quạt làm mát', tv.fan_speed !== undefined ? `${tv.fan_speed}%` : '--'],
      ['Cầu nối', p.device_id || '--'],
      ['Cập nhật', relTime(p.last_seen)],
    ].forEach(([k, v]) => { const pair = el('div', 'metric-pair'); pair.append(el('dt', '', k), el('dd', 'num', v)); kv.appendChild(pair); });

    const job = $('detail-job'); job.textContent = '';
    const heading = el('div', 'job-heading'); const title = el('div'); title.append(el('div', 'eyebrow', status === 'idle' ? 'TÁC VỤ GẦN NHẤT' : 'TÁC VỤ HIỆN TẠI'), el('h3', '', t.job_name || 'Chưa có tác vụ được báo cáo'));
    heading.append(title, el('strong', 'job-percent num', typeof t.progress === 'number' ? `${t.progress}%` : '—'));
    const progress = el('div', 'job-progress'); progress.setAttribute('role', 'progressbar'); progress.setAttribute('aria-label', 'Tiến độ in'); progress.setAttribute('aria-valuemin', '0'); progress.setAttribute('aria-valuemax', '100'); if (typeof t.progress === 'number') progress.setAttribute('aria-valuenow', String(t.progress)); const fill = el('span'); fill.style.width = `${Math.max(0, Math.min(100, Number(t.progress) || 0))}%`; progress.appendChild(fill);
    const timeline = el('div', 'job-timeline'); timeline.append(el('span', '', t.current_layer == null ? 'Chưa có dữ liệu lớp' : `Lớp ${t.current_layer} / ${t.total_layers ?? '—'}`), el('span', '', status === 'idle' && t.progress === 100 ? 'Đã hoàn thành' : status === 'printing' && t.remaining_min != null ? minutes(t.remaining_min) : STATUS[status]));
    job.append(heading, progress, timeline);
    renderControls(p);
    renderFilament(p);

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
    f.elements.post_print_review.checked = st.post_print_review !== false;
    f.elements.failure_action.value = st.failure_action || 'notify';
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
    ['vision_enabled', 'capture_when_idle', 'post_print_review'].forEach((k) => {
      if (f.elements[k].checked !== Boolean(st[k])) patch[k] = f.elements[k].checked;
    });
    if (f.elements.failure_action.value !== (st.failure_action || 'notify')) patch.failure_action = f.elements.failure_action.value;
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
      applyExpectations();
      checkWebVersion(state.data.config.web_version);
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

  // The dashboard is a long-lived tab: pick up new dashboard files on its own,
  // but never in the middle of something (settings form, live view, command).
  function checkWebVersion(v) {
    if (!v) return;
    if (!state.webVersion) { state.webVersion = v; return; }
    if (v === state.webVersion) return;
    const busy = $('settings').open || $('login').open || state.live || Object.keys(state.cmds).length
      || ($('detail').open && !$('detail').classList.contains('docked'))
      || $('upload-btn').disabled || viewers.size;
    if (!busy) location.reload();
  }

  function schedule() {
    clearInterval(state.timer);
    state.timer = setInterval(() => { if (!document.hidden) sync(); }, REFRESH_MS);
  }

  function renderUser() {
    const chip = $('user-chip');
    $('btn-settings').hidden = !(state.tsUser || state.token);
    $('user-name').textContent = state.tsUser || (state.token ? 'Mã quản lý' : '');
    $('user-method').textContent = state.tsUser ? 'Tailscale' : (state.token ? 'Đăng nhập bằng mã' : '');
    if (state.tsUser) {
      chip.textContent = state.tsUser.slice(0, 1);
      chip.title = `${state.tsUser} (Tailscale)`;
      chip.setAttribute('aria-label', `Đăng nhập: ${state.tsUser}`);
      chip.hidden = false;
      $('btn-logout').hidden = true;
    } else {
      chip.textContent = '';
      chip.appendChild(icon('key-round'));
      chip.title = 'Đăng nhập bằng mã quản lý';
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

  // ---------------------------------------------------------------------------
  // Theme (auto / light / dark), remembered per browser
  // ---------------------------------------------------------------------------
  const THEME_KEY = 'printpilot_theme';
  function currentTheme() {
    const t = document.documentElement.getAttribute('data-theme');
    return t === 'light' || t === 'dark' ? t : 'auto';
  }
  function applyTheme(choice) {
    if (choice === 'light' || choice === 'dark') {
      document.documentElement.setAttribute('data-theme', choice);
      try { localStorage.setItem(THEME_KEY, choice); } catch { /* ignore */ }
    } else {
      document.documentElement.removeAttribute('data-theme');
      try { localStorage.removeItem(THEME_KEY); } catch { /* ignore */ }
    }
    renderThemeSwitch();
  }
  function renderThemeSwitch() {
    const t = currentTheme();
    document.querySelectorAll('#btn-theme [data-icon]').forEach((svg) => { svg.toggleAttribute('hidden', svg.dataset.icon !== t); });
    document.querySelectorAll('[data-theme-choice]').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.themeChoice === t)));
    $('btn-theme').dataset.tip = `Giao diện: ${{ auto: 'Theo hệ thống', light: 'Sáng', dark: 'Tối' }[t]}`;
  }
  function bindTheme() {
    const btn = $('btn-theme');
    const menu = $('theme-menu');
    const close = () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
      btn.setAttribute('aria-expanded', String(!menu.hidden));
      if (!menu.hidden) menu.querySelector('[aria-checked="true"]')?.focus();
    });
    menu.querySelectorAll('[data-theme-choice]').forEach((b) => b.addEventListener('click', () => {
      applyTheme(b.dataset.themeChoice);
      close();
      btn.focus();
    }));
    document.addEventListener('click', (e) => { if (!menu.hidden && !menu.contains(e.target)) close(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !menu.hidden) { close(); btn.focus(); } });
    renderThemeSwitch();
  }

  // ---------------------------------------------------------------------------
  // Tabs (hash routes: #printers, #alerts, #ai, #log)
  // ---------------------------------------------------------------------------
  const TABS = ['printers', 'alerts', 'ai', 'filaments', 'care', 'log'];
  const TAB_TITLE = { printers: 'Máy in', alerts: 'Cảnh báo', ai: 'Tối ưu thông số', filaments: 'Nhựa in', care: 'Bảo trì', log: 'Nhật ký' };
  function currentTab() {
    const h = location.hash.replace(/^#\/?/, '');
    return TABS.includes(h) ? h : 'printers';
  }
  function showTab() {
    const tab = currentTab();
    document.querySelectorAll('.view[data-view]').forEach((v) => { v.hidden = v.dataset.view !== tab; });
    document.querySelectorAll('.nav-item[data-tab]').forEach((a) => {
      if (a.dataset.tab === tab) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    document.title = tab === 'printers' ? 'PrintPilot' : `${TAB_TITLE[tab]} · PrintPilot`;
    if (tab !== 'printers' && state.live) stopLive();
    if (tab === 'filaments') { if (!fil.data) loadFilaments(); loadSpools(); }
    if (tab === 'care') loadCare();
    window.scrollTo(0, 0);
  }

  // ---------------------------------------------------------------------------
  // Filament library (#filaments): presets per printer model, custom/imported presets,
  // .bbsflmt bundles for Bambu Studio (File > Import > Import Configs)
  // ---------------------------------------------------------------------------
  const fil = { data: null, model: '', selected: new Set(), editing: null };
  const FIL_SOURCE = { system: ['Có sẵn trong Studio', 'tag'], added: ['PrintPilot thêm', 'tag tag-ok'], custom: ['Tự tạo', 'tag tag-warn'], import: ['Đã nhập', 'tag tag-warn'] };
  const MODEL_CODE = { 'Bambu Lab A1': 'A1', 'Bambu Lab A1 mini': 'A1M', 'Bambu Lab X1 Carbon': 'X1C', 'Bambu Lab X1': 'X1', 'Bambu Lab X1E': 'X1E', 'Bambu Lab P1S': 'P1S', 'Bambu Lab P1P': 'P1P', 'Bambu Lab P2S': 'P2S', 'Bambu Lab H2D': 'H2D', 'Bambu Lab H2S': 'H2S', 'Bambu Lab H2C': 'H2C', 'Bambu Lab X2D': 'X2D' };
  const FIL_FIELDS = ['nozzle_temperature', 'nozzle_temperature_initial_layer', 'nozzle_temperature_range_low', 'nozzle_temperature_range_high', 'textured_plate_temp', 'hot_plate_temp', 'cool_plate_temp', 'filament_max_volumetric_speed', 'filament_flow_ratio', 'fan_min_speed', 'fan_max_speed', 'filament_density', 'filament_cost'];
  const presetKey = (p) => (p.id ? `id:${p.id}` : `name:${p.name}`);
  const presetSource = (p) => (p.source === 'system' ? (p.in_stock_studio ? 'system' : 'added') : p.source);

  function filStatus(text, kind) {
    const status = $('fil-status');
    status.hidden = !text; status.textContent = text || ''; status.dataset.kind = kind || '';
  }

  async function loadFilaments(model) {
    filStatus('Đang tải danh sách nhựa…');
    try {
      const params = new URLSearchParams({ nozzle: $('fil-nozzle').value });
      if (model) params.set('printer_model', model);
      const query = `?${params}`;
      fil.data = await (await api(`/api/filament-library${query}`)).json();
      fil.model = fil.data.printer_model;
      const models = [...new Set([...(fil.data.printer_models || []), fil.model, ...Object.keys(MODEL_CODE)])];
      const select = $('fil-printer'); select.textContent = '';
      models.forEach((m) => { const o = el('option', '', m.replace('Bambu Lab ', '')); o.value = m; o.selected = m === fil.model; select.appendChild(o); });
      const vendor = $('fil-vendor'); const keep = vendor.value; vendor.textContent = ''; vendor.appendChild(el('option', '', 'Tất cả hãng')).value = '';
      (fil.data.vendors || []).forEach((v) => { const o = el('option', '', `${v.vendor} (${v.presets})`); o.value = v.vendor; o.selected = v.vendor === keep; vendor.appendChild(o); });
      const own = fil.data.types.reduce((n, t) => n + t.presets.filter((p) => p.source !== 'system').length, 0);
      $('nav-fil-count').hidden = !own; $('nav-fil-count').textContent = String(own);
      filStatus('');
      renderFilaments();
    } catch (err) {
      filStatus('Không tải được danh sách nhựa. Hãy thử lại.', 'error');
      if (err instanceof AuthError) handleError(err);
    }
  }

  function filMatches(p, type) {
    const q = $('fil-search').value.trim().toLowerCase();
    const vendor = $('fil-vendor').value;
    if (vendor && p.vendor !== vendor) return false;
    if ($('fil-only-export').checked && !p.exportable) return false;
    return !q || `${p.name} ${p.vendor} ${type.type} ${type.label}`.toLowerCase().includes(q);
  }

  function specText(sp) {
    const parts = [];
    if (sp.nozzle_temperature) parts.push(`Đầu phun ${sp.nozzle_temperature}°C` + (sp.nozzle_temperature_range_low ? ` (${sp.nozzle_temperature_range_low}–${sp.nozzle_temperature_range_high})` : ''));
    const bed = [sp.textured_plate_temp ? `Textured ${sp.textured_plate_temp}` : '', sp.hot_plate_temp ? `PEI trơn ${sp.hot_plate_temp}` : '', sp.cool_plate_temp ? `Cool ${sp.cool_plate_temp}` : ''].filter(Boolean);
    if (bed.length) parts.push(`Bàn ${bed.join(' · ')}°C`);
    if (sp.filament_max_volumetric_speed) parts.push(`≤ ${sp.filament_max_volumetric_speed} mm³/s`);
    if (sp.fan_max_speed !== undefined) parts.push(`Quạt ${sp.fan_min_speed ?? '?'}–${sp.fan_max_speed}%`);
    return parts.join(' · ');
  }

  function renderFilaments() {
    const root = $('fil-types'); root.textContent = '';
    let shown = 0;
    (fil.data ? fil.data.types : []).forEach((t) => {
      const items = t.presets.filter((p) => filMatches(p, t));
      if (!items.length) return;
      shown += items.length;
      const card = el('details', 'fil-type');
      card.open = !!$('fil-search').value || $('fil-only-export').checked || items.some((p) => p.source !== 'system' || fil.selected.has(presetKey(p))) || (t.popular && items.length <= 12);
      const head = el('summary');
      const title = el('div', 'fil-type-title');
      title.append(el('strong', '', t.label), el('span', 'muted', `${items.length} preset`));
      const badges = el('div', 'fil-badges');
      if (t.hygroscopic >= 2) badges.appendChild(el('span', 'tag tag-warn', t.hygroscopic >= 3 ? 'Hút ẩm rất mạnh' : 'Hút ẩm'));
      if (t.needs_enclosure) badges.appendChild(el('span', 'tag tag-danger', 'Cần buồng kín'));
      if (t.abrasive) badges.appendChild(el('span', 'tag tag-danger', 'Cần đầu phun thép cứng'));
      head.append(title, badges);
      card.appendChild(head);
      if (t.description) card.appendChild(el('p', 'fil-desc', t.description));
      const list = el('ul', 'fil-presets');
      items.forEach((p) => {
        const li = el('li', 'fil-preset');
        const pick = el('input'); pick.type = 'checkbox'; pick.disabled = !p.exportable; pick.checked = fil.selected.has(presetKey(p));
        pick.setAttribute('aria-label', p.exportable ? `Chọn ${p.name}` : `${p.name} đã có sẵn trong Studio`);
        pick.addEventListener('change', () => { if (pick.checked) fil.selected.add(presetKey(p)); else fil.selected.delete(presetKey(p)); updateFilExport(); });
        const info = el('div', 'fil-info');
        const top = el('div', 'fil-name');
        const source = FIL_SOURCE[presetSource(p)] || FIL_SOURCE.system;
        top.append(el('b', '', p.name), el('span', source[1], source[0]));
        info.append(top, el('small', 'muted', [p.vendor, specText(p.specs || {}), p.base && p.source !== 'system' ? `gốc: ${p.base}` : ''].filter(Boolean).join(' · ')));
        if (p.notes && p.source === 'custom') info.appendChild(el('small', 'fil-notes', p.notes));
        const actions = el('div', 'fil-row-actions');
        if (p.source === 'system' && p.in_stock_studio) {
          const derive = el('button', 'btn btn-quiet', 'Tạo bản riêng'); derive.type = 'button';
          derive.title = 'Tạo preset của hãng khác dựa trên preset này';
          derive.addEventListener('click', () => openFilDialog({ base: p.name }));
          actions.appendChild(derive);
        }
        if (p.source === 'custom') {
          const edit = el('button', 'btn btn-quiet', 'Sửa'); edit.type = 'button';
          edit.addEventListener('click', () => openFilDialog(p));
          actions.appendChild(edit);
        }
        if ((p.source === 'system' && p.in_stock_studio) || p.source === 'custom') {
          const calib = el('button', 'btn btn-quiet', 'Hiệu chỉnh'); calib.type = 'button';
          calib.title = 'Ghi kết quả hiệu chỉnh trên máy của bạn vào preset';
          calib.addEventListener('click', () => openCalibration(p.name));
          actions.appendChild(calib);
        }
        if (p.id) {
          const del = el('button', 'btn btn-quiet fil-delete', 'Xóa'); del.type = 'button';
          del.addEventListener('click', async () => {
            if (!confirm(`Xóa «${p.name}» khỏi thư viện? Preset đã nhập vào Bambu Studio vẫn giữ nguyên.`)) return;
            try { await postJson(`/api/filament-library/${encodeURIComponent(p.id)}/delete`, {}); fil.selected.delete(presetKey(p)); await loadFilaments(fil.model); }
            catch (err) { filStatus('Không xóa được preset.', 'error'); if (err instanceof AuthError) handleError(err); }
          });
          actions.appendChild(del);
        }
        li.append(pick, info, actions);
        list.appendChild(li);
      });
      card.appendChild(list);
      if (t.notes && t.notes.length) { const notes = el('ul', 'material-notes'); t.notes.forEach((n) => notes.appendChild(el('li', '', n))); card.appendChild(notes); }
      root.appendChild(card);
    });
    $('fil-empty').hidden = shown > 0 || !fil.data;
    updateFilExport();
  }

  function updateFilExport() {
    const btn = $('fil-export');
    btn.disabled = !fil.selected.size;
    btn.querySelector('span').textContent = fil.selected.size ? `Tải về cho Bambu Studio (${fil.selected.size})` : 'Tải về cho Bambu Studio';
  }

  async function exportFilaments() {
    const params = new URLSearchParams();
    fil.selected.forEach((k) => { const [kind, ...rest] = k.split(':'); params.append(kind, rest.join(':')); });
    try {
      const res = await api(`/api/filament-library/export?${params}`);
      const blob = await res.blob();
      const match = /filename\*=UTF-8''([^;]+)/.exec(res.headers.get('Content-Disposition') || '');
      const a = el('a'); a.href = URL.createObjectURL(blob); a.download = match ? decodeURIComponent(match[1]) : 'printpilot-filaments.bbsflmt';
      document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      filStatus(`Đã tải ${fil.selected.size} preset. Mở Bambu Studio → File → Import → Import Configs… và chọn file .bbsflmt.`, 'ok');
    } catch (err) {
      filStatus('Không tạo được file. Hãy thử lại.', 'error');
      if (err instanceof AuthError) handleError(err);
    }
  }

  function baseSpecs(name) {
    for (const t of fil.data.types) { const p = t.presets.find((x) => x.name === name && x.source === 'system'); if (p) return p; }
    return null;
  }

  function suggestName(form) {
    if (fil.editing && fil.editing.id) return;
    const code = MODEL_CODE[fil.model] || fil.model.replace('Bambu Lab ', '');
    const vendor = form.elements.vendor.value.trim(); const product = form.elements.product.value.trim();
    const base = baseSpecs(form.elements.base.value);
    form.elements.name.value = [vendor, product || (base ? base.type : '')].filter(Boolean).join(' ') + (vendor ? ` @BBL ${code}` : '');
  }

  function fillFromBase(form) {
    const base = baseSpecs(form.elements.base.value);
    FIL_FIELDS.forEach((f) => { form.elements[f].placeholder = base && base.specs[f] !== undefined ? String(base.specs[f]) : ''; });
  }

  function openFilDialog(preset) {
    const dialog = $('fil-dialog'); const form = $('fil-form');
    form.reset(); $('fil-form-error').hidden = true;
    fil.editing = preset && preset.id ? preset : null;
    const select = form.elements.base; select.textContent = '';
    fil.data.types.forEach((t) => {
      const group = el('optgroup'); group.label = t.label;
      t.presets.filter((p) => p.source === 'system' && p.in_stock_studio).forEach((p) => { const o = el('option', '', p.name); o.value = p.name; group.appendChild(o); });
      if (group.children.length) select.appendChild(group);
    });
    const generic = (fil.data.bases || []).find((b) => /^Generic PETG/.test(b));
    select.value = (preset && preset.base) || generic || select.value;
    $('fil-dialog-title').textContent = fil.editing ? `Sửa «${preset.name}»` : 'Thêm nhựa vào thư viện';
    if (fil.editing) {
      form.elements.vendor.value = preset.vendor || ''; form.elements.name.value = preset.name; form.elements.notes.value = preset.notes || '';
      const base = baseSpecs(preset.base) || { specs: {} };
      FIL_FIELDS.forEach((f) => { const v = (preset.specs || {})[f]; if (v !== undefined && v !== base.specs[f]) form.elements[f].value = v; });
    }
    fillFromBase(form);
    if (!fil.editing) suggestName(form);
    dialog.showModal();
  }

  async function saveFilament(e) {
    e.preventDefault();
    const form = $('fil-form'); const error = $('fil-form-error');
    const values = {};
    FIL_FIELDS.forEach((f) => { if (form.elements[f].value !== '') values[f] = Number(form.elements[f].value); });
    const body = { name: form.elements.name.value.trim(), vendor: form.elements.vendor.value.trim(), base: form.elements.base.value, values, notes: form.elements.notes.value.trim() };
    if (form.elements.colour.value && form.elements.colour.value.toUpperCase() !== '#FFFFFF') body.colour = form.elements.colour.value;
    if (fil.editing) body.id = fil.editing.id;
    $('fil-save').disabled = true;
    try {
      const { status, data } = await postJsonAllowError('/api/filament-library', body);
      if (status !== 200) { error.textContent = data.error || 'Không lưu được preset.'; error.hidden = false; return; }
      $('fil-dialog').close();
      fil.selected.add(`id:${data.preset.id}`);
      await loadFilaments(fil.model);
      filStatus(`Đã lưu «${data.preset.name}». Bấm “Tải về cho Bambu Studio” để nhập vào Studio.`, 'ok');
    } catch (err) {
      error.textContent = 'Không lưu được preset.'; error.hidden = false;
      if (err instanceof AuthError) handleError(err);
    } finally { $('fil-save').disabled = false; }
  }

  async function importFilamentFiles(files) {
    const messages = [];
    for (const file of files) {
      try {
        const data_base64 = await readBase64(file);
        const { status, data } = await postJsonAllowError('/api/filament-library/import', { filename: file.name, data_base64 });
        if (status !== 200) { messages.push(`${file.name}: ${data.error || 'lỗi'}`); continue; }
        data.imported.forEach((p) => fil.selected.add(`id:${p.id}`));
        messages.push(`${file.name}: nhập ${data.imported.length} preset` + (data.skipped.length ? `, bỏ qua ${data.skipped.length} (${data.skipped.slice(0, 3).map((s) => `${s.name}: ${s.reason}`).join('; ')})` : ''));
      } catch (err) {
        messages.push(`${file.name}: không đọc được file`);
        if (err instanceof AuthError) { handleError(err); return; }
      }
    }
    await loadFilaments(fil.model);
    filStatus(messages.join(' · '), messages.some((m) => /lỗi|không|bỏ qua/.test(m)) ? 'error' : 'ok');
  }

  function bindFilaments() {
    $('fil-printer').addEventListener('change', (e) => { fil.selected.clear(); loadFilaments(e.target.value); });
    $('fil-nozzle').addEventListener('change', () => { fil.selected.clear(); loadFilaments(fil.model); });
    ['fil-search', 'fil-vendor', 'fil-only-export'].forEach((id) => $(id).addEventListener(id === 'fil-search' ? 'input' : 'change', () => fil.data && renderFilaments()));
    $('fil-export').addEventListener('click', exportFilaments);
    $('fil-add').addEventListener('click', () => fil.data && openFilDialog(null));
    $('fil-import').addEventListener('click', () => $('fil-import-input').click());
    $('fil-import-input').addEventListener('change', (e) => { const files = [...e.target.files]; e.target.value = ''; if (files.length) importFilamentFiles(files); });
    const form = $('fil-form');
    form.addEventListener('submit', saveFilament);
    $('fil-cancel').addEventListener('click', () => $('fil-dialog').close());
    ['vendor', 'product'].forEach((n) => form.elements[n].addEventListener('input', () => suggestName(form)));
    form.elements.base.addEventListener('change', () => { fillFromBase(form); suggestName(form); });
  }

  // ---------------------------------------------------------------------------
  // Spools (inside #filaments) and filament calibration
  // ---------------------------------------------------------------------------
  const spools = { data: null, editing: null };
  const CAL_FIELDS = ['nozzle_temperature', 'pressure_advance', 'filament_flow_ratio', 'filament_retraction_length', 'filament_max_volumetric_speed'];
  const slotKey = (printerId, amsId, slotId) => `${printerId}|${amsId}|${slotId}`;

  async function loadSpools() {
    try {
      spools.data = await (await api('/api/spools')).json();
      renderSpools();
    } catch (err) {
      $('spool-list').textContent = 'Không tải được danh sách cuộn nhựa.';
      if (err instanceof AuthError) handleError(err);
    }
  }

  function renderSpools() {
    const root = $('spool-list'); root.textContent = '';
    const list = (spools.data && spools.data.spools) || [];
    if (!list.length) { root.appendChild(el('p', 'muted', 'Chưa có cuộn nào. Thêm cuộn để theo dõi khối lượng còn lại và lịch sấy.')); return; }
    const slots = new Map(((spools.data && spools.data.slots) || []).map((s) => [slotKey(s.printer_id, s.ams_id, s.slot_id), s]));
    list.forEach((sp) => {
      const card = el('article', 'spool-card');
      const swatch = el('span', 'spool-swatch'); swatch.style.background = sp.colour || 'var(--surface-3)';
      const info = el('div', 'spool-info');
      const top = el('div', 'fil-name');
      top.append(el('b', '', sp.name));
      if (sp.needs_drying) top.appendChild(el('span', 'tag tag-danger', `Nên sấy (${sp.days_since_dry} ngày)`));
      else if (sp.dry_after_days) top.appendChild(el('span', 'tag', sp.days_since_dry == null ? 'Chưa ghi ngày mở/sấy' : `${sp.days_since_dry}/${sp.dry_after_days} ngày từ lần sấy`));
      const slot = sp.printer_id != null ? slots.get(slotKey(sp.printer_id, sp.ams_id, sp.slot_id)) : null;
      const where = sp.printer_id != null ? `${slot ? `${slot.printer} · ${slot.label}` : `${printerLabel(sp.printer_id)} · khay ${sp.slot_id + 1}`}` : 'Chưa gắn khay';
      info.append(top, el('small', 'muted', [sp.vendor, sp.material || sp.polymer, sp.preset, where].filter(Boolean).join(' · ')));
      if (sp.total_g) {
        const pct = Math.max(0, Math.min(100, Math.round(((sp.remaining_g ?? sp.total_g) / sp.total_g) * 100)));
        const bar = el('div', 'spool-bar'); const fill = el('span'); fill.style.width = `${pct}%`;
        if (pct < 15) bar.classList.add('low');
        bar.appendChild(fill);
        bar.setAttribute('role', 'img'); bar.setAttribute('aria-label', `Còn ${pct}%`);
        info.append(bar, el('small', 'muted', `Còn ${Math.round(sp.remaining_g ?? sp.total_g)} / ${Math.round(sp.total_g)} g`));
      }
      if (sp.notes) info.appendChild(el('small', 'fil-notes', sp.notes));
      const actions = el('div', 'fil-row-actions');
      const dried = el('button', 'btn btn-quiet', 'Vừa sấy'); dried.type = 'button';
      dried.addEventListener('click', async () => {
        try { await postJson(`/api/spools/${encodeURIComponent(sp.id)}/dried`, {}); await loadSpools(); filStatus(`Đã ghi lần sấy cho «${sp.name}».`, 'ok'); }
        catch (err) { filStatus('Không lưu được.', 'error'); if (err instanceof AuthError) handleError(err); }
      });
      const edit = el('button', 'btn btn-quiet', 'Sửa'); edit.type = 'button';
      edit.addEventListener('click', () => openSpoolDialog(sp));
      const cal = el('button', 'btn btn-quiet', 'Hiệu chỉnh'); cal.type = 'button'; cal.disabled = !sp.preset;
      cal.title = sp.preset ? `Hiệu chỉnh preset «${sp.preset}»` : 'Chọn preset cho cuộn để hiệu chỉnh';
      cal.addEventListener('click', () => openCalibration(sp.preset, sp.printer_id));
      const archive = el('button', 'btn btn-quiet fil-delete', 'Dùng hết'); archive.type = 'button';
      archive.addEventListener('click', async () => {
        if (!confirm(`Đánh dấu «${sp.name}» đã dùng hết và bỏ khỏi khay?`)) return;
        try { await postJson(`/api/spools/${encodeURIComponent(sp.id)}/archive`, {}); await loadSpools(); }
        catch (err) { filStatus('Không lưu được.', 'error'); if (err instanceof AuthError) handleError(err); }
      });
      actions.append(dried, edit, cal, archive);
      card.append(swatch, info, actions);
      root.appendChild(card);
    });
  }

  function allPresetNames() {
    const names = [];
    ((fil.data && fil.data.types) || []).forEach((t) => t.presets.forEach((p) => names.push(p.name)));
    return [...new Set(names)];
  }

  function openSpoolDialog(sp) {
    const form = $('spool-form'); form.reset(); $('spool-form-error').hidden = true;
    spools.editing = sp && sp.id ? sp : null;
    $('spool-dialog-title').textContent = spools.editing ? `Sửa «${sp.name}»` : 'Thêm cuộn nhựa';
    const mats = $('spool-materials'); mats.textContent = '';
    ((fil.data && fil.data.types) || []).forEach((t) => { const o = el('option'); o.value = t.type; mats.appendChild(o); });
    const presets = $('spool-presets'); presets.textContent = '';
    allPresetNames().forEach((n) => { const o = el('option'); o.value = n; presets.appendChild(o); });
    const select = form.elements.slot; select.textContent = '';
    const none = el('option', '', 'Chưa gắn khay'); none.value = ''; select.appendChild(none);
    ((spools.data && spools.data.slots) || []).forEach((s) => {
      const o = el('option', '', `${s.printer} · ${s.label}${s.material ? ` (${s.material})` : ''}`);
      o.value = slotKey(s.printer_id, s.ams_id, s.slot_id); select.appendChild(o);
    });
    if (sp) {
      ['name', 'vendor', 'material', 'preset', 'total_g', 'remaining_g', 'opened_at', 'dried_at', 'notes'].forEach((k) => { if (sp[k] != null) form.elements[k].value = sp[k]; });
      if (sp.colour) form.elements.colour.value = sp.colour;
      if (sp.printer_id != null) {
        const key = slotKey(sp.printer_id, sp.ams_id, sp.slot_id);
        if (![...select.options].some((o) => o.value === key)) { const o = el('option', '', `${printerLabel(sp.printer_id)} · khay ${sp.slot_id + 1}`); o.value = key; select.appendChild(o); }
        select.value = key;
      }
    } else {
      form.elements.opened_at.value = new Date().toISOString().slice(0, 10);
    }
    $('spool-dialog').showModal();
  }

  async function saveSpool(e) {
    e.preventDefault();
    const form = $('spool-form'); const error = $('spool-form-error');
    const body = {};
    ['name', 'vendor', 'material', 'preset', 'opened_at', 'dried_at', 'notes'].forEach((k) => { const v = form.elements[k].value.trim(); if (v) body[k] = v; });
    ['total_g', 'remaining_g'].forEach((k) => { if (form.elements[k].value !== '') body[k] = Number(form.elements[k].value); });
    if (form.elements.colour.value.toUpperCase() !== '#FFFFFF' || (spools.editing && spools.editing.colour)) body.colour = form.elements.colour.value.toUpperCase();
    if (form.elements.slot.value) {
      const [printerId, amsId, slotId] = form.elements.slot.value.split('|');
      Object.assign(body, { printer_id: printerId, ams_id: Number(amsId), slot_id: Number(slotId) });
    }
    if (spools.editing) body.id = spools.editing.id;
    try {
      const { status, data } = await postJsonAllowError('/api/spools', body);
      if (status !== 200) { error.textContent = data.error || 'Không lưu được cuộn.'; error.hidden = false; return; }
      $('spool-dialog').close();
      await loadSpools();
      filStatus(`Đã lưu cuộn «${data.spool.name}».`, 'ok');
    } catch (err) {
      error.textContent = 'Không lưu được cuộn.'; error.hidden = false;
      if (err instanceof AuthError) handleError(err);
    }
  }

  const cal = { preset: null };
  function fmtSuggest(s) {
    if (!s) return '';
    const parts = [];
    if (s.current != null) parts.push(`đang dùng ${s.current}`);
    if (s.from != null && s.to != null) parts.push(`thử ${s.from} → ${s.to}, bước ${s.step}`);
    (s.current_on_printer || []).forEach((k) => parts.push(`${k.slot}: K ${k.k}${Number.isInteger(k.cali_idx) && k.cali_idx >= 0 ? ` (profile #${k.cali_idx})` : ''}`));
    return parts.join(' · ');
  }

  async function openCalibration(preset, printerId) {
    const form = $('cal-form'); form.reset(); $('cal-form-error').hidden = true;
    cal.preset = preset;
    $('cal-dialog-title').textContent = `Hiệu chỉnh «${preset}»`;
    const steps = $('cal-steps'); steps.textContent = 'Đang tải hướng dẫn…';
    $('cal-dialog').showModal();
    try {
      const params = new URLSearchParams({ preset });
      if (printerId) params.set('printer_id', printerId);
      const guide = await (await api(`/api/calibration/guide?${params}`)).json();
      steps.textContent = '';
      guide.steps.forEach((s) => {
        const li = el('li');
        li.append(el('b', '', s.title), el('p', '', s.how));
        const hint = fmtSuggest(s.suggest);
        if (hint) li.appendChild(el('small', 'muted', hint));
        steps.appendChild(li);
        const input = form.elements[s.key];
        if (input && s.suggest && s.suggest.current != null) input.placeholder = String(s.suggest.current);
      });
      if (guide.note) steps.appendChild(el('p', 'muted', guide.note));
    } catch (err) {
      steps.textContent = 'Không tải được hướng dẫn. Vẫn có thể nhập kết quả bên dưới.';
      if (err instanceof AuthError) handleError(err);
    }
  }

  async function saveCalibration(e) {
    e.preventDefault();
    const form = $('cal-form'); const error = $('cal-form-error');
    const values = {};
    CAL_FIELDS.forEach((f) => { if (form.elements[f].value !== '') values[f] = Number(form.elements[f].value); });
    if (!Object.keys(values).length) { error.textContent = 'Nhập ít nhất một kết quả.'; error.hidden = false; return; }
    try {
      const { status, data } = await postJsonAllowError('/api/calibration', { preset: cal.preset, values, notes: form.elements.notes.value.trim() });
      if (status !== 200) { error.textContent = data.error || 'Không lưu được kết quả.'; error.hidden = false; return; }
      $('cal-dialog').close();
      if (data.preset && data.preset.id) fil.selected.add(`id:${data.preset.id}`);
      await loadFilaments(fil.model);
      filStatus(`Đã lưu vào «${data.preset.name}». Bấm “Tải về cho Bambu Studio” để dùng preset đã hiệu chỉnh.`, 'ok');
    } catch (err) {
      error.textContent = 'Không lưu được kết quả.'; error.hidden = false;
      if (err instanceof AuthError) handleError(err);
    }
  }

  function bindSpools() {
    $('spool-add').addEventListener('click', () => openSpoolDialog(null));
    $('spool-form').addEventListener('submit', saveSpool);
    $('spool-cancel').addEventListener('click', () => $('spool-dialog').close());
    $('cal-form').addEventListener('submit', saveCalibration);
    $('cal-cancel').addEventListener('click', () => $('cal-dialog').close());
  }

  // ---------------------------------------------------------------------------
  // Printer care (#care): maintenance by real print hours, room environment
  // ---------------------------------------------------------------------------
  const care = { maintenance: null, environment: null };

  function careStatus(text, kind) {
    const status = $('care-status');
    status.hidden = !text; status.textContent = text || ''; status.dataset.kind = kind || '';
  }

  function updateCareBadge() {
    const due = ((care.maintenance && care.maintenance.printers) || []).reduce((n, p) => n + p.tasks.filter((t) => t.due).length, 0);
    $('nav-care-count').hidden = !due; $('nav-care-count').textContent = String(due);
  }

  async function loadCare(render = true) {
    try {
      const [m, env] = await Promise.all([api('/api/maintenance').then((r) => r.json()), api('/api/environment').then((r) => r.json())]);
      care.maintenance = m; care.environment = env;
      updateCareBadge();
      if (render) renderCare();
    } catch (err) {
      if (render) careStatus('Không tải được dữ liệu bảo trì.', 'error');
      if (err instanceof AuthError) handleError(err);
    }
  }

  function renderCare() {
    careStatus('');
    const root = $('care-printers'); root.textContent = '';
    const printers = (care.maintenance && care.maintenance.printers) || [];
    if (!printers.length) root.appendChild(el('p', 'muted', 'Chưa có máy in nào kết nối.'));
    printers.forEach((p) => {
      const card = el('section', 'care-card');
      const head = el('div', 'care-head');
      head.append(el('h2', '', p.name || p.printer_id), el('span', 'muted', p.model || ''));
      card.appendChild(head);
      const list = el('ul', 'care-tasks');
      p.tasks.forEach((t) => {
        const li = el('li', `care-task${t.due ? ' due' : ''}`);
        const info = el('div', 'fil-info');
        const top = el('div', 'fil-name'); top.appendChild(el('b', '', t.label));
        if (t.due) top.appendChild(el('span', 'tag tag-danger', 'Đến hạn'));
        else if (t.ratio >= 0.8) top.appendChild(el('span', 'tag tag-warn', 'Sắp đến hạn'));
        const bar = el('div', 'spool-bar care-bar'); const fill = el('span'); fill.style.width = `${Math.min(100, Math.round(t.ratio * 100))}%`;
        if (t.due) bar.classList.add('low');
        bar.appendChild(fill); bar.setAttribute('role', 'img'); bar.setAttribute('aria-label', `${Math.round(t.ratio * 100)}% chu kỳ`);
        const last = t.last_done ? `lần trước ${relTime(t.last_done)}` : 'chưa ghi lần nào (tính từ lần in đầu)';
        info.append(top, bar, el('small', 'muted', `${t.hours} / ${t.interval_hours} giờ in · ${last}`), el('small', 'fil-notes', t.why));
        const actions = el('div', 'fil-row-actions');
        const done = el('button', `btn${t.due ? ' btn-primary' : ''}`, 'Đã làm'); done.type = 'button';
        done.addEventListener('click', async () => {
          done.disabled = true;
          try { await postJson(`/api/maintenance/${encodeURIComponent(p.printer_id)}/${t.task}/done`, {}); await loadCare(); careStatus(`Đã ghi: ${t.label}.`, 'ok'); }
          catch (err) { careStatus('Không lưu được.', 'error'); if (err instanceof AuthError) handleError(err); }
          finally { done.disabled = false; }
        });
        const interval = el('button', 'btn btn-quiet', 'Đổi chu kỳ'); interval.type = 'button';
        interval.addEventListener('click', async () => {
          const value = prompt(`Chu kỳ «${t.label}» (giờ in, 1–5000):`, String(t.interval_hours));
          if (value === null) return;
          try {
            const { status, data } = await postJsonAllowError(`/api/maintenance/${encodeURIComponent(p.printer_id)}/${t.task}/interval`, { hours: Number(value) });
            if (status !== 200) { careStatus(data.error || 'Chu kỳ không hợp lệ.', 'error'); return; }
            await loadCare();
          } catch (err) { careStatus('Không lưu được.', 'error'); if (err instanceof AuthError) handleError(err); }
        });
        actions.append(done, interval);
        li.append(info, actions);
        list.appendChild(li);
      });
      card.appendChild(list);
      root.appendChild(card);
    });

    const env = $('care-env'); env.textContent = '';
    const sensors = (care.environment && care.environment.sensors) || [];
    if (!sensors.length) {
      env.append(el('p', 'muted', 'Chưa có cảm biến gửi số liệu trong 24 giờ qua.'),
        el('p', 'muted', 'Gắn cảm biến nhiệt/ẩm (VD: ESP32 + SHT31) và gửi POST /api/environment bằng mã thiết bị, body {"sensor_id": "phong-in", "temperature": 27.5, "humidity": 68, "printer_id": "(tùy chọn)"}, khoảng 5 phút một lần.'));
      return;
    }
    const grid = el('div', 'env-grid');
    sensors.forEach((s) => {
      const card = el('div', 'env-card');
      const l = s.latest || {};
      card.append(el('b', '', s.sensor_id), el('small', 'muted', s.printer_id ? `gắn với ${printerLabel(s.printer_id)}` : 'cả phòng'));
      const now = el('div', 'env-now');
      now.append(el('span', 'num', l.temperature != null ? `${l.temperature.toFixed(1)}°C` : '—'), el('span', 'num', l.humidity != null ? `${Math.round(l.humidity)}%` : '—'));
      card.appendChild(now);
      const temps = s.series.map((r) => r[1]).filter((v) => v != null); const hums = s.series.map((r) => r[2]).filter((v) => v != null);
      const range = (a, unit) => (a.length ? `${Math.min(...a).toFixed(unit === '%' ? 0 : 1)}–${Math.max(...a).toFixed(unit === '%' ? 0 : 1)}${unit}` : '—');
      card.appendChild(el('small', 'muted', `24 giờ: ${range(temps, '°C')} · ${range(hums, '%')} · cập nhật ${relTime(l.created_at)}`));
      if (l.humidity != null && l.humidity >= 60) card.appendChild(el('small', 'env-warn', 'Phòng ẩm: nhựa hút ẩm (PETG, TPU, nylon) nên để trong hộp sấy hoặc hộp kín có hút ẩm.'));
      if (l.temperature != null && l.temperature < 18) card.appendChild(el('small', 'env-warn', 'Phòng lạnh: tránh gió lùa, tăng nhiệt bàn cho lớp đầu, cân nhắc che chắn máy.'));
      grid.appendChild(card);
    });
    env.appendChild(grid);
  }

  function bind() {
    showTab();
    window.addEventListener('hashchange', showTab);
    bindTheme();
    bindSettings();
    bindUpload();
    bindFilaments();
    bindSpools();
    ['ev-printer', 'ev-category', 'ev-problems'].forEach((id) => $(id).addEventListener('change', () => loadEvents(true)));
    $('events-more').addEventListener('click', () => loadEvents(false));
    document.querySelectorAll('.seg[data-filter]').forEach((b) => b.addEventListener('click', () => {
      state.alertFilter = b.dataset.filter;
      document.querySelectorAll('.seg[data-filter]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
      renderAlerts();
    }));

    const detail = $('detail');
    detail.querySelector('[data-close]').addEventListener('click', () => detail.close());
    detail.addEventListener('click', (e) => { if (e.target === detail && !detail.classList.contains('docked')) detail.close(); });
    WIDE.addEventListener('change', onLayoutChange);
    detail.addEventListener('close', () => { stopLive(); renameEditing(false); state.detailPrinterId = null; markSelected(); });
    $('btn-rename').addEventListener('click', startRename);
    $('rename-form').addEventListener('submit', submitRename);
    $('rename-cancel').addEventListener('click', () => renameEditing(false));
    $('rename-input').addEventListener('keydown', (e) => {
      // Escape cancels the edit instead of closing the whole dialog
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); renameEditing(false); }
    });
    $('btn-live').addEventListener('click', () => {
      if (state.live && state.live.printerId === state.detailPrinterId) stopLive();
      else if (state.detailPrinterId) startLive(state.detailPrinterId);
    });
    window.addEventListener('pagehide', () => stopLive());
    detail.querySelectorAll('[data-cmd]').forEach((b) => b.addEventListener('click', () => runCommand(b, state.detailPrinterId)));

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
    if (currentTab() !== 'care') loadCare(false);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
