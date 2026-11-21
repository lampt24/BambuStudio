/**
 * PrintPilot Dashboard - Main Application Script
 * Vanilla JavaScript (ES6+), Vietnamese UI, Strict XSS Prevention (textContent)
 * Authenticated Bearer Token & Blob Image Handling
 */

(function () {
  'use strict';

  // --- Constants & Storage Keys ---
  const STORAGE_KEY_TOKEN = 'printpilot_token';
  const STORAGE_KEY_REFRESH_INTERVAL = 'printpilot_refresh_interval';

  // --- Application State ---
  const state = {
    token: sessionStorage.getItem(STORAGE_KEY_TOKEN) || '',
    // Tailscale identity (set from /api/whoami); when present no token is needed
    tsUser: '',
    refreshIntervalMs: parseInt(sessionStorage.getItem(STORAGE_KEY_REFRESH_INTERVAL) || '10000', 10),
    refreshTimer: null,
    isSyncing: false,
    lastSyncTime: null,
    data: {
      analyses: [],
      snapshots: [],
      alerts: [],
      devices: [],
      printers: [],
      config: {
        ai_configured: false,
        vision_configured: false,
        notifications_configured: false
      }
    },
    selectedSnapshotId: null,
    selectedPrinterId: null,
    // true only after the user picks an older thumbnail; otherwise follow the newest
    snapshotPinned: false,
    alertFilter: 'unack', // 'unack' | 'all'
    activeObjectUrls: new Set()
  };

  // --- DOM Elements Cache ---
  const dom = {
    // Config pills
    cfgAi: document.getElementById('cfg-ai'),
    cfgVision: document.getElementById('cfg-vision'),
    cfgNotify: document.getElementById('cfg-notify'),

    // Sync & Refresh
    syncIndicator: document.getElementById('sync-indicator'),
    syncText: document.getElementById('sync-text'),
    autoRefreshSelect: document.getElementById('auto-refresh-select'),
    btnManualRefresh: document.getElementById('btn-manual-refresh'),
    iconRefresh: document.getElementById('icon-refresh'),

    // Auth & Header
    authInfo: document.getElementById('auth-info'),
    authTokenPreview: document.getElementById('auth-token-preview'),
    btnLogout: document.getElementById('btn-logout'),
    btnShowLogin: document.getElementById('btn-show-login'),

    // Global Banner
    globalBanner: document.getElementById('global-banner'),
    bannerMessage: document.getElementById('banner-message'),
    bannerRetryBtn: document.getElementById('banner-retry-btn'),

    // Mobile Nav
    mobileNavBtns: document.querySelectorAll('.mobile-nav-btn'),
    mobileAlertCount: document.getElementById('mobile-alert-count'),

    // Summary Metrics
    statCamFreshness: document.getElementById('stat-cam-freshness'),
    statCamTime: document.getElementById('stat-cam-time'),
    statAlertsBadge: document.getElementById('stat-alerts-badge'),
    statAlertsCount: document.getElementById('stat-alerts-count'),
    statAnalysesCount: document.getElementById('stat-analyses-count'),
    statDevicesBadge: document.getElementById('stat-devices-badge'),
    statDevicesCount: document.getElementById('stat-devices-count'),

    // Camera Section
    cameraFreshnessBadge: document.getElementById('camera-freshness-badge'),
    headingCamera: document.getElementById('heading-camera'),
    printersGrid: document.getElementById('printers-grid'),
    printersEmpty: document.getElementById('printers-empty'),
    printersCountBadge: document.getElementById('printers-count-badge'),
    btnExpandSnapshot: document.getElementById('btn-expand-snapshot'),
    cameraLoading: document.getElementById('camera-loading'),
    cameraEmpty: document.getElementById('camera-empty'),
    cameraCurrentImg: document.getElementById('camera-current-img'),
    cameraHud: document.getElementById('camera-hud'),
    hudDeviceId: document.getElementById('hud-device-id'),
    hudPrinterId: document.getElementById('hud-printer-id'),
    hudCapturedTime: document.getElementById('hud-captured-time'),
    hudCapturedRelative: document.getElementById('hud-captured-relative'),
    hudServerTime: document.getElementById('hud-server-time'),

    // Camera Telemetry
    cameraTelemetryPanel: document.getElementById('camera-telemetry-panel'),
    telemetryStatus: document.getElementById('telemetry-status'),
    telemetryNozzle: document.getElementById('telemetry-nozzle'),
    telemetryBed: document.getElementById('telemetry-bed'),
    telemetryProgress: document.getElementById('telemetry-progress'),
    telemetryLayer: document.getElementById('telemetry-layer'),
    telemetryFan: document.getElementById('telemetry-fan'),

    // Vision Assessment
    visionAssessmentBox: document.getElementById('vision-assessment-box'),
    visionAssessmentText: document.getElementById('vision-assessment-text'),

    // Snapshot Thumbnails
    snapshotThumbnails: document.getElementById('snapshot-thumbnails'),
    thumbnailsEmpty: document.getElementById('thumbnails-empty'),
    historyCount: document.getElementById('history-count'),

    // Devices Section
    devicesContainer: document.getElementById('devices-container'),
    devicesEmpty: document.getElementById('devices-empty'),

    // Alerts Section
    filterTabs: document.querySelectorAll('.filter-tab'),
    unackCount: document.getElementById('unack-count'),
    alertsList: document.getElementById('alerts-list'),
    alertsEmpty: document.getElementById('alerts-empty'),

    // Analyses Section
    analysesCountBadge: document.getElementById('analyses-count-badge'),
    analysesList: document.getElementById('analyses-list'),
    analysesEmpty: document.getElementById('analyses-empty'),

    // Footer
    footerClientTime: document.getElementById('footer-client-time'),

    // Login Modal
    loginModal: document.getElementById('login-modal'),
    loginForm: document.getElementById('login-form'),
    tokenInput: document.getElementById('token-input'),
    btnToggleToken: document.getElementById('btn-toggle-token'),
    loginError: document.getElementById('login-error'),
    btnCancelLogin: document.getElementById('btn-cancel-login'),
    btnSubmitLogin: document.getElementById('btn-submit-login'),
    loginSpinner: document.getElementById('login-spinner'),

    // Lightbox Modal
    lightboxModal: document.getElementById('lightbox-modal'),
    lightboxImg: document.getElementById('lightbox-img'),
    lightboxSubtitle: document.getElementById('lightbox-subtitle'),
    btnCloseLightbox: document.getElementById('btn-close-lightbox'),
    lightboxTelemetry: document.getElementById('lightbox-telemetry'),

    // Toast Container
    toastContainer: document.getElementById('toast-container')
  };

  // --- Helper: Formatters (Vietnamese) ---

  /**
   * Safe date parsing and formatting
   * Returns: "HH:mm:ss DD/MM/YYYY"
   */
  function formatDateTime(isoString) {
    if (!isoString) return '--:--:--';
    try {
      const d = new Date(isoString);
      if (isNaN(d.getTime())) return String(isoString);
      const pad = (n) => String(n).padStart(2, '0');
      const hours = pad(d.getHours());
      const mins = pad(d.getMinutes());
      const secs = pad(d.getSeconds());
      const day = pad(d.getDate());
      const month = pad(d.getMonth() + 1);
      const year = d.getFullYear();
      return `${hours}:${mins}:${secs} ${day}/${month}/${year}`;
    } catch {
      return String(isoString);
    }
  }

  /**
   * Format time only: "HH:mm:ss"
   */
  function formatTime(isoString) {
    if (!isoString) return '--:--:--';
    try {
      const d = new Date(isoString);
      if (isNaN(d.getTime())) return '--:--:--';
      const pad = (n) => String(n).padStart(2, '0');
      return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    } catch {
      return '--:--:--';
    }
  }

  /**
   * Relative time string in Vietnamese
   */
  function formatRelativeTime(isoString) {
    if (!isoString) return 'Chưa có thông tin';
    try {
      const d = new Date(isoString);
      if (isNaN(d.getTime())) return '';
      const diffSec = Math.round((Date.now() - d.getTime()) / 1000);
      if (diffSec < 0) return 'Vừa xong';
      if (diffSec < 10) return 'Vừa xong';
      if (diffSec < 60) return `${diffSec} giây trước`;
      const diffMin = Math.floor(diffSec / 60);
      if (diffMin < 60) return `${diffMin} phút trước`;
      const diffHour = Math.floor(diffMin / 60);
      if (diffHour < 24) return `${diffHour} giờ trước`;
      const diffDay = Math.floor(diffHour / 24);
      return `${diffDay} ngày trước`;
    } catch {
      return '';
    }
  }

  /**
   * Translate printing status to Vietnamese
   */
  function translateStatus(status) {
    const s = String(status || '').toLowerCase();
    switch (s) {
      case 'printing':
        return { label: 'Đang in', className: 'badge-cyan' };
      case 'idle':
        return { label: 'Đang rảnh', className: 'badge-green' };
      case 'paused':
        return { label: 'Tạm dừng', className: 'badge-amber' };
      case 'complete':
        return { label: 'Hoàn thành', className: 'badge-green' };
      case 'error':
        return { label: 'Lỗi', className: 'badge-red' };
      case 'unknown':
      default:
        return { label: 'Không rõ', className: 'badge-unknown' };
    }
  }

  /**
   * Translate AI objective
   */
  function translateObjective(obj) {
    const o = String(obj || '').toLowerCase();
    switch (o) {
      case 'quality':
        return 'Chất lượng cao';
      case 'strength':
        return 'Độ bền cơ học';
      case 'speed':
        return 'Tốc độ tối đa';
      case 'economy':
        return 'Tiết kiệm vật liệu';
      default:
        return obj || 'Mặc định';
    }
  }

  /**
   * Translate AI trigger
   */
  function translateTrigger(trig) {
    const t = String(trig || '').toLowerCase();
    switch (t) {
      case 'manual':
        return 'Thủ công';
      case 'send':
        return 'Gửi lệnh in';
      default:
        return trig || 'Khác';
    }
  }

  /**
   * Translate Alert Severity
   */
  function translateSeverity(sev) {
    const s = String(sev || '').toLowerCase();
    switch (s) {
      case 'critical':
      case 'error':
        return { label: 'Nghiêm trọng', className: 'badge-red', cardClass: 'alert-item--critical' };
      case 'warning':
        return { label: 'Cảnh báo', className: 'badge-amber', cardClass: 'alert-item--warning' };
      case 'info':
      default:
        return { label: 'Thông tin', className: 'badge-cyan', cardClass: 'alert-item--info' };
    }
  }

  /**
   * Safe JSON parse helper
   */
  function safeJsonParse(val, fallback) {
    if (val === null || val === undefined) return fallback;
    if (typeof val === 'object') return val;
    if (typeof val === 'string') {
      try {
        return JSON.parse(val);
      } catch {
        return fallback;
      }
    }
    return fallback;
  }

  // --- Helper: Toast Notification ---
  function showToast(message, type = 'info') {
    if (!dom.toastContainer) return;
    const toast = document.createElement('div');
    toast.className = `toast toast--${type}`;
    toast.setAttribute('role', 'status');

    const text = document.createElement('span');
    text.textContent = message;
    toast.appendChild(text);

    dom.toastContainer.appendChild(toast);

    setTimeout(() => {
      toast.style.transition = 'opacity 200ms ease-out, transform 200ms ease-out';
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(100%)';
      setTimeout(() => {
        if (toast.parentNode) {
          toast.parentNode.removeChild(toast);
        }
      }, 200);
    }, 4000);
  }

  // --- Memory Management: Object URLs for Authenticated Blobs ---
  function createTrackedBlobUrl(blob) {
    const url = URL.createObjectURL(blob);
    state.activeObjectUrls.add(url);
    return url;
  }

  function revokeAllBlobUrls() {
    state.activeObjectUrls.forEach((url) => {
      try {
        URL.revokeObjectURL(url);
      } catch (e) {
        // ignore
      }
    });
    state.activeObjectUrls.clear();
  }

  // --- API Client Layer ---

  function getApiUrl(path) {
    // Restrict strictly to same-origin relative endpoints to prevent credential leakage to arbitrary hosts
    return path.startsWith('/') ? path : '/' + path;
  }

  function isAuthenticated() {
    return Boolean(state.token || state.tsUser);
  }

  async function apiFetch(path, options = {}) {
    if (!isAuthenticated()) {
      throw new Error('NO_TOKEN');
    }

    const url = getApiUrl(path);
    const headers = new Headers(options.headers || {});
    if (state.token) {
      headers.set('Authorization', `Bearer ${state.token}`);
    }
    // Same-origin marker required by the server for Tailscale-authenticated writes
    headers.set('X-PrintPilot-Request', '1');

    if (options.body && typeof options.body === 'string' && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }

    const res = await fetch(url, {
      ...options,
      headers
    });

    if (res.status === 401 || res.status === 403) {
      throw new Error('UNAUTHORIZED');
    }

    if (!res.ok) {
      const errText = await res.text().catch(() => '');
      throw new Error(`HTTP ${res.status}: ${errText || res.statusText}`);
    }

    return res;
  }

  /**
   * Fetch image as authenticated blob
   */
  async function fetchAuthenticatedImageBlob(imageId) {
    if (!imageId) throw new Error('MISSING_IMAGE_ID');
    const res = await apiFetch(`/api/images/${encodeURIComponent(imageId)}`);
    const blob = await res.blob();
    return createTrackedBlobUrl(blob);
  }

  /**
   * Fetch current system state
   */
  async function fetchState() {
    const res = await apiFetch('/api/state');
    return await res.json();
  }

  /**
   * Acknowledge alert
   */
  async function ackAlert(alertId) {
    const res = await apiFetch(`/api/alerts/${encodeURIComponent(alertId)}/ack`, {
      method: 'POST'
    });
    return await res.json().catch(() => ({}));
  }

  // --- UI Update & Rendering Logic ---

  function updateAuthUI() {
    if (state.tsUser) {
      dom.authInfo.style.display = 'flex';
      dom.btnShowLogin.style.display = 'none';
      dom.authTokenPreview.textContent = state.tsUser;
      dom.authTokenPreview.title = 'Đăng nhập qua tài khoản Tailscale';
      dom.btnLogout.style.display = 'none';
    } else if (state.token) {
      dom.authInfo.style.display = 'flex';
      dom.btnShowLogin.style.display = 'none';
      dom.btnLogout.style.display = '';

      // Safe static indicator - never expose token fragments in header
      dom.authTokenPreview.textContent = 'Đã xác thực';
    } else {
      dom.authInfo.style.display = 'none';
      dom.btnShowLogin.style.display = 'inline-flex';
    }
  }

  function showBanner(message, isError = true) {
    dom.globalBanner.style.display = 'block';
    dom.bannerMessage.textContent = message;
    if (isError) {
      dom.globalBanner.style.background = 'linear-gradient(90deg, rgba(244, 63, 94, 0.2), rgba(244, 63, 94, 0.1))';
      dom.bannerRetryBtn.style.display = 'inline-flex';
    } else {
      dom.globalBanner.style.background = 'linear-gradient(90deg, rgba(245, 158, 11, 0.2), rgba(245, 158, 11, 0.1))';
      dom.bannerRetryBtn.style.display = 'none';
    }
  }

  function hideBanner() {
    dom.globalBanner.style.display = 'none';
  }

  function updateSyncIndicator(status, message) {
    dom.syncIndicator.className = 'sync-indicator';
    if (status === 'syncing') {
      dom.syncIndicator.classList.add('sync-indicator--syncing');
      dom.iconRefresh.classList.add('is-spinning');
    } else if (status === 'ok') {
      dom.syncIndicator.classList.add('sync-indicator--ok');
      dom.iconRefresh.classList.remove('is-spinning');
    } else {
      dom.syncIndicator.classList.add('sync-indicator--error');
      dom.iconRefresh.classList.remove('is-spinning');
    }
    if (message) {
      dom.syncText.textContent = message;
    }
  }

  function renderConfigPills(config) {
    const setPill = (el, isConfigured, name) => {
      const valEl = el.querySelector('.config-val');
      if (isConfigured) {
        el.className = 'config-pill config-pill--on';
        el.setAttribute('title', `${name}: Đã cấu hình và sẵn sàng`);
        valEl.textContent = 'Sẵn sàng';
      } else {
        el.className = 'config-pill config-pill--off';
        el.setAttribute('title', `${name}: Chưa được cấu hình`);
        valEl.textContent = 'Chưa bật';
      }
    };

    setPill(dom.cfgAi, Boolean(config && config.ai_configured), 'Phân tích AI');
    setPill(dom.cfgVision, Boolean(config && config.vision_configured), 'Thị giác máy tính');
    setPill(dom.cfgNotify, Boolean(config && config.notifications_configured), 'Webhook thông báo');
  }

  /**
   * Check if alert is acknowledged (handles boolean or SQLite 0/1 integer)
   */
  function isAlertAcked(alert) {
    return Boolean(alert.acknowledged && alert.acknowledged !== 0 && alert.acknowledged !== '0');
  }

  /**
   * Render Top Summary Cards
   */
  function renderSummaryMetrics() {
    const { snapshots, alerts, analyses, devices } = state.data;

    // Camera snapshot freshness
    const latestSnapshot = snapshots && snapshots.length > 0 ? snapshots[0] : null;
    if (latestSnapshot) {
      const captureTime = latestSnapshot.captured_at || latestSnapshot.created_at;
      dom.statCamTime.textContent = formatTime(captureTime);

      const diffSec = captureTime ? Math.round((Date.now() - new Date(captureTime).getTime()) / 1000) : Infinity;
      if (diffSec <= 150) {
        dom.statCamFreshness.className = 'badge badge-green';
        dom.statCamFreshness.textContent = 'Mới (<2m)';
      } else if (diffSec <= 360) {
        dom.statCamFreshness.className = 'badge badge-amber';
        dom.statCamFreshness.textContent = 'Chậm trễ';
      } else {
        dom.statCamFreshness.className = 'badge badge-unknown';
        dom.statCamFreshness.textContent = 'Không rõ / Cũ';
      }
    } else {
      dom.statCamTime.textContent = '--:--:--';
      dom.statCamFreshness.className = 'badge badge-muted';
      dom.statCamFreshness.textContent = 'Chưa có ảnh';
    }

    // Active unacknowledged alerts (neutral: no alerts does not mean safe)
    const unackedAlerts = (alerts || []).filter((a) => !isAlertAcked(a));
    dom.statAlertsCount.textContent = String(unackedAlerts.length);
    if (unackedAlerts.length === 0) {
      dom.statAlertsBadge.className = 'badge badge-muted';
      dom.statAlertsBadge.textContent = 'Không có cảnh báo';
    } else {
      dom.statAlertsBadge.className = 'badge badge-red';
      dom.statAlertsBadge.textContent = `${unackedAlerts.length} cần xử lý`;
    }

    // Mobile badge
    if (dom.mobileAlertCount) {
      if (unackedAlerts.length > 0) {
        dom.mobileAlertCount.textContent = String(unackedAlerts.length);
        dom.mobileAlertCount.style.display = 'inline-block';
      } else {
        dom.mobileAlertCount.style.display = 'none';
      }
    }

    // AI analyses
    dom.statAnalysesCount.textContent = String((analyses || []).length);

    // Devices
    const connectedDevices = devices || [];
    dom.statDevicesCount.textContent = String(connectedDevices.length);
    if (connectedDevices.length > 0) {
      const hasError = connectedDevices.some((d) => (d.last_status || d.status) === 'error' || d.last_error || d.error);
      if (hasError) {
        dom.statDevicesBadge.className = 'badge badge-red';
        dom.statDevicesBadge.textContent = 'Có lỗi';
      } else {
        dom.statDevicesBadge.className = 'badge badge-green';
        dom.statDevicesBadge.textContent = 'Hoạt động';
      }
    } else {
      dom.statDevicesBadge.className = 'badge badge-muted';
      dom.statDevicesBadge.textContent = 'Chưa kết nối';
    }
  }

  /**
   * Render Camera Snapshot & Telemetry
   */
  function selectedPrinter() {
    return (state.data.printers || []).find((p) => p.printer_id === state.selectedPrinterId) || null;
  }

  function printerLabel(p) {
    return (p && (p.name || p.printer_id)) || '';
  }

  // Snapshots of the selected printer (all snapshots when no printer is known yet)
  function visibleSnapshots() {
    const all = state.data.snapshots || [];
    if (!state.selectedPrinterId) return all;
    return all.filter((s) => s.printer_id === state.selectedPrinterId);
  }

  function formatMinutes(min) {
    if (min === undefined || min === null || isNaN(min)) return '--';
    const m = Math.max(0, Math.round(min));
    return m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}` : `${m} phút`;
  }

  function fmtTemp(v) {
    return typeof v === 'number' ? `${Math.round(v)}°C` : '--';
  }

  /**
   * Render printer fleet cards
   */
  function renderPrinters() {
    const printers = state.data.printers || [];
    dom.printersCountBadge.textContent = `${printers.length} máy`;
    dom.printersEmpty.style.display = printers.length ? 'none' : 'block';
    dom.printersGrid.textContent = '';

    if (printers.length && !printers.some((p) => p.printer_id === state.selectedPrinterId)) {
      state.selectedPrinterId = printers[0].printer_id;
      state.snapshotPinned = false;
    }

    printers.forEach((p) => {
      const tele = p.telemetry || {};
      const status = p.stale ? { label: 'Mất kết nối', className: 'badge-unknown' } : translateStatus(p.status);

      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'printer-card';
      card.setAttribute('role', 'option');
      card.setAttribute('aria-selected', String(p.printer_id === state.selectedPrinterId));

      const head = document.createElement('div');
      head.className = 'printer-card-head';
      const titleWrap = document.createElement('div');
      titleWrap.style.minWidth = '0';
      const name = document.createElement('div');
      name.className = 'printer-card-name';
      name.textContent = printerLabel(p);
      const meta = document.createElement('div');
      meta.className = 'printer-card-meta font-mono';
      meta.textContent = [p.model, p.printer_id !== printerLabel(p) ? p.printer_id : '', `cập nhật ${formatRelativeTime(p.last_seen)}`]
        .filter(Boolean).join(' · ');
      titleWrap.appendChild(name);
      titleWrap.appendChild(meta);
      const badge = document.createElement('span');
      badge.className = `badge ${status.className}`;
      badge.textContent = status.label;
      head.appendChild(titleWrap);
      head.appendChild(badge);
      card.appendChild(head);

      if (tele.job_name) {
        const job = document.createElement('div');
        job.className = 'printer-card-job';
        job.textContent = tele.job_name;
        job.title = tele.job_name;
        card.appendChild(job);
      }

      if (typeof tele.progress === 'number') {
        const bar = document.createElement('div');
        bar.className = 'printer-progress';
        bar.setAttribute('role', 'progressbar');
        bar.setAttribute('aria-valuenow', String(tele.progress));
        bar.setAttribute('aria-valuemin', '0');
        bar.setAttribute('aria-valuemax', '100');
        const fill = document.createElement('div');
        fill.className = 'printer-progress-bar';
        fill.style.width = `${Math.min(100, Math.max(0, tele.progress))}%`;
        bar.appendChild(fill);
        card.appendChild(bar);
      }

      const stats = document.createElement('div');
      stats.className = 'printer-card-stats';
      [
        ['Tiến độ', typeof tele.progress === 'number' ? `${tele.progress}%` : '--'],
        ['Còn lại', formatMinutes(tele.remaining_min)],
        ['Lớp', tele.current_layer !== undefined ? `${tele.current_layer}/${tele.total_layers ?? '?'}` : '--'],
        ['Đầu in', fmtTemp(tele.nozzle_temp)],
        ['Bàn in', fmtTemp(tele.bed_temp)],
        ['Quạt', tele.fan_speed !== undefined ? `${tele.fan_speed}%` : '--'],
      ].forEach(([label, value]) => {
        const cell = document.createElement('div');
        const l = document.createElement('span');
        l.className = 'printer-stat-label';
        l.textContent = label;
        const v = document.createElement('span');
        v.className = 'printer-stat-value';
        v.textContent = value;
        cell.appendChild(l);
        cell.appendChild(v);
        stats.appendChild(cell);
      });
      card.appendChild(stats);

      if (p.last_error) {
        const err = document.createElement('div');
        err.className = 'printer-card-error';
        err.textContent = p.last_error;
        card.appendChild(err);
      }

      card.addEventListener('click', () => {
        if (state.selectedPrinterId === p.printer_id) return;
        state.selectedPrinterId = p.printer_id;
        state.snapshotPinned = false;
        renderPrinters();
        renderCameraSection();
      });
      dom.printersGrid.appendChild(card);
    });
  }

  async function renderCameraSection() {
    const snapshots = visibleSnapshots();
    const printer = selectedPrinter();
    dom.headingCamera.textContent = printer ? `Camera: ${printerLabel(printer)}` : 'Camera Giám Sát In 3D';

    if (snapshots.length === 0) {
      dom.cameraEmpty.style.display = 'flex';
      dom.cameraCurrentImg.style.display = 'none';
      dom.cameraHud.style.display = 'none';
      dom.cameraTelemetryPanel.style.display = 'none';
      dom.visionAssessmentBox.style.display = 'none';
      dom.btnExpandSnapshot.disabled = true;
      dom.cameraFreshnessBadge.className = 'badge badge-unknown';
      dom.cameraFreshnessBadge.textContent = 'Không có ảnh (Không rõ)';
      renderThumbnails([]);
      return;
    }

    // Determine which snapshot to display: selected or latest
    let currentSnapshot = state.snapshotPinned
      ? snapshots.find((s) => s.id === state.selectedSnapshotId)
      : null;
    if (!currentSnapshot) {
      currentSnapshot = snapshots[0];
      state.selectedSnapshotId = currentSnapshot.id;
      state.snapshotPinned = false;
    }

    dom.cameraEmpty.style.display = 'none';
    dom.btnExpandSnapshot.disabled = false;

    // Check Freshness based on captured_at (Rule: No fresh image means unknown, never healthy)
    const capturedTime = currentSnapshot.captured_at || currentSnapshot.created_at;
    let diffSec = Infinity;
    if (capturedTime) {
      const d = new Date(capturedTime);
      if (!isNaN(d.getTime())) {
        diffSec = Math.round((Date.now() - d.getTime()) / 1000);
      }
    }

    // Update freshness badge
    dom.cameraFreshnessBadge.className = 'badge';
    if (diffSec <= 150) {
      dom.cameraFreshnessBadge.classList.add('badge-green');
      dom.cameraFreshnessBadge.textContent = 'Trực tiếp (< 2 phút)';
    } else if (diffSec <= 360) {
      dom.cameraFreshnessBadge.classList.add('badge-amber');
      dom.cameraFreshnessBadge.textContent = `Chậm trễ (${Math.round(diffSec / 60)} phút trước)`;
    } else {
      // PLAN.md rule: "No fresh image means unknown, never healthy"
      dom.cameraFreshnessBadge.classList.add('badge-unknown');
      dom.cameraFreshnessBadge.textContent = 'Không rõ (Ảnh quá hạn)';
    }

    // Populate HUD
    dom.hudDeviceId.textContent = `Cầu nối: ${currentSnapshot.device_id || 'redmi-9c'}`;
    dom.hudPrinterId.textContent = `Máy in: ${printer ? printerLabel(printer) : (currentSnapshot.printer_id || 'không rõ')}`;
    dom.hudCapturedTime.textContent = formatTime(capturedTime);
    dom.hudCapturedRelative.textContent = formatRelativeTime(capturedTime);
    dom.hudServerTime.textContent = formatDateTime(currentSnapshot.created_at);
    dom.cameraHud.style.display = 'flex';

    // Telemetry: live printer numbers when following the newest image,
    // otherwise the numbers recorded with the selected (older) snapshot
    const snapTele = safeJsonParse(currentSnapshot.telemetry, {});
    const liveTele = printer && !state.snapshotPinned ? (printer.telemetry || {}) : {};
    const tele = Object.keys(liveTele).length ? liveTele : snapTele;
    const liveStatus = printer && !state.snapshotPinned && !printer.stale ? printer.status : currentSnapshot.status;
    dom.telemetryStatus.textContent = translateStatus(liveStatus).label;

    dom.telemetryNozzle.textContent = tele.nozzle_temp !== undefined ? `${tele.nozzle_temp} °C` : '--';
    dom.telemetryBed.textContent = tele.bed_temp !== undefined ? `${tele.bed_temp} °C` : '--';
    dom.telemetryProgress.textContent = tele.progress !== undefined ? `${tele.progress} %` : '--';

    if (tele.current_layer !== undefined && tele.total_layers !== undefined) {
      dom.telemetryLayer.textContent = `${tele.current_layer} / ${tele.total_layers}`;
    } else if (tele.current_layer !== undefined) {
      dom.telemetryLayer.textContent = String(tele.current_layer);
    } else {
      dom.telemetryLayer.textContent = '--';
    }

    dom.telemetryFan.textContent = tele.fan_speed !== undefined ? `${tele.fan_speed} %` : '--';
    dom.cameraTelemetryPanel.style.display = 'grid';

    // Populate Vision Box if vision_result or vision_status exists (ensuring vision_result can be an object)
    const visionRes = safeJsonParse(currentSnapshot.vision_result, currentSnapshot.vision_result);
    if (visionRes || currentSnapshot.vision_status) {
      dom.visionAssessmentBox.style.display = 'block';
      let visionSummary = '';
      if (typeof visionRes === 'string') {
        visionSummary = visionRes;
      } else if (visionRes && typeof visionRes === 'object') {
        if (visionRes.summary) {
          visionSummary = visionRes.summary;
        } else if (visionRes.description) {
          visionSummary = visionRes.description;
        } else if (visionRes.message) {
          visionSummary = visionRes.message;
        } else if (visionRes.status) {
          visionSummary = `Trạng thái: ${visionRes.status}`;
        } else if (visionRes.issue || visionRes.error) {
          visionSummary = `Sự cố: ${visionRes.issue || visionRes.error}`;
        } else {
          try {
            visionSummary = Object.entries(visionRes)
              .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`)
              .join(' | ');
          } catch {
            visionSummary = JSON.stringify(visionRes);
          }
        }
      } else if (currentSnapshot.vision_status) {
        visionSummary = `Trạng thái thị giác: ${currentSnapshot.vision_status}`;
      } else {
        visionSummary = 'Chưa có thông tin chi tiết';
      }
      dom.visionAssessmentText.textContent = visionSummary;
    } else {
      dom.visionAssessmentBox.style.display = 'none';
    }

    // Fetch and display authenticated image blob
    dom.cameraLoading.style.display = 'flex';
    dom.cameraCurrentImg.style.display = 'none';

    try {
      const blobUrl = await fetchAuthenticatedImageBlob(currentSnapshot.id);
      dom.cameraCurrentImg.src = blobUrl;
      dom.cameraCurrentImg.onload = () => {
        dom.cameraLoading.style.display = 'none';
        dom.cameraCurrentImg.style.display = 'block';
      };
      dom.cameraCurrentImg.onerror = () => {
        dom.cameraLoading.style.display = 'none';
        dom.cameraCurrentImg.style.display = 'none';
        dom.cameraEmpty.style.display = 'flex';
      };
    } catch (err) {
      dom.cameraLoading.style.display = 'none';
      console.warn('Không thể tải blob hình ảnh camera:', err);
    }

    // Render thumbnail history strip
    renderThumbnails(snapshots);
  }

  /**
   * Render Snapshot Thumbnails Strip
   */
  function renderThumbnails(snapshots) {
    dom.snapshotThumbnails.textContent = ''; // clear safely

    if (snapshots.length === 0) {
      const p = document.createElement('p');
      p.className = 'text-muted text-sm';
      p.textContent = 'Chưa có lịch sử ảnh chụp';
      dom.snapshotThumbnails.appendChild(p);
      dom.historyCount.textContent = '0 ảnh';
      return;
    }

    dom.historyCount.textContent = `${snapshots.length} ảnh`;

    snapshots.slice(0, 10).forEach((snap) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'thumb-item';
      if (snap.id === state.selectedSnapshotId) {
        item.classList.add('active');
      }
      item.setAttribute('aria-label', `Xem ảnh chụp lúc ${formatDateTime(snap.captured_at || snap.created_at)}`);

      // Time tag
      const timeTag = document.createElement('span');
      timeTag.className = 'thumb-time font-mono';
      timeTag.textContent = formatTime(snap.captured_at || snap.created_at);

      item.appendChild(timeTag);

      item.addEventListener('click', () => {
        state.selectedSnapshotId = snap.id;
        // Clicking the newest thumbnail resumes live follow mode
        state.snapshotPinned = snap.id !== snapshots[0].id;
        renderCameraSection();
      });

      dom.snapshotThumbnails.appendChild(item);
    });
  }

  /**
   * Render Connected Devices
   */
  function renderDevices() {
    const devices = state.data.devices || [];
    dom.devicesContainer.textContent = ''; // clear

    if (devices.length === 0) {
      const emptyDiv = document.createElement('div');
      emptyDiv.className = 'empty-placeholder';

      const pTitle = document.createElement('p');
      pTitle.className = 'empty-title';
      pTitle.textContent = 'Chưa có thiết bị nào kết nối';

      const pDesc = document.createElement('p');
      pDesc.className = 'empty-desc';
      pDesc.textContent = 'Máy chủ chưa nhận được nhịp tim (heartbeat) từ cầu nối camera Redmi 9C hoặc máy in Bambu A1.';

      emptyDiv.appendChild(pTitle);
      emptyDiv.appendChild(pDesc);
      dom.devicesContainer.appendChild(emptyDiv);
      return;
    }

    devices.forEach((dev) => {
      const card = document.createElement('div');
      card.className = 'device-card';

      // Header
      const header = document.createElement('div');
      header.className = 'device-header';

      const ids = document.createElement('div');
      ids.className = 'device-ids';

      const title = document.createElement('span');
      title.className = 'device-title';
      title.textContent = dev.printer_id || dev.device_id || 'Thiết bị';

      const devIdBadge = document.createElement('span');
      devIdBadge.className = 'badge badge-navy font-mono';
      devIdBadge.textContent = dev.device_id || 'N/A';

      ids.appendChild(title);
      ids.appendChild(devIdBadge);

      // Status (supports dev.last_status, dev.status)
      const devStatus = dev.last_status || dev.status || 'unknown';
      const statusInfo = translateStatus(devStatus);
      const statusBadge = document.createElement('span');
      statusBadge.className = `badge ${statusInfo.className}`;
      statusBadge.textContent = statusInfo.label;

      header.appendChild(ids);
      header.appendChild(statusBadge);
      card.appendChild(header);

      // Metrics grid
      const grid = document.createElement('div');
      grid.className = 'device-details-grid';

      // Heartbeat metric (supports dev.last_seen, dev.last_heartbeat)
      const lastSeen = dev.last_seen || dev.last_heartbeat;
      const m1 = document.createElement('div');
      m1.className = 'device-metric';
      const m1Lbl = document.createElement('span');
      m1Lbl.className = 'device-metric-lbl';
      m1Lbl.textContent = 'Nhịp tim gần nhất';
      const m1Val = document.createElement('span');
      m1Val.className = 'device-metric-val font-mono';
      m1Val.textContent = lastSeen ? formatRelativeTime(lastSeen) : 'Chưa có';
      m1.appendChild(m1Lbl);
      m1.appendChild(m1Val);

      // Printer model / id
      const m2 = document.createElement('div');
      m2.className = 'device-metric';
      const m2Lbl = document.createElement('span');
      m2Lbl.className = 'device-metric-lbl';
      m2Lbl.textContent = 'Mã máy in';
      const m2Val = document.createElement('span');
      m2Val.className = 'device-metric-val font-mono';
      m2Val.textContent = dev.printer_id || 'Bambu A1';
      m2.appendChild(m2Lbl);
      m2.appendChild(m2Val);

      // Telemetry if present
      const devTele = safeJsonParse(dev.telemetry, null);
      if (devTele && typeof devTele === 'object') {
        if (devTele.progress !== undefined) {
          const mProg = document.createElement('div');
          mProg.className = 'device-metric';
          const lbl = document.createElement('span');
          lbl.className = 'device-metric-lbl';
          lbl.textContent = 'Tiến độ';
          const val = document.createElement('span');
          val.className = 'device-metric-val font-mono';
          val.textContent = `${devTele.progress} %`;
          mProg.appendChild(lbl);
          mProg.appendChild(val);
          grid.appendChild(mProg);
        }
      }

      grid.appendChild(m1);
      grid.appendChild(m2);
      card.appendChild(grid);

      // Device reported error (supports dev.last_error, dev.error)
      const devErr = dev.last_error || dev.error;
      if (devErr) {
        const errBanner = document.createElement('div');
        errBanner.className = 'device-error-banner';
        errBanner.textContent = `Lỗi thiết bị: ${devErr}`;
        card.appendChild(errBanner);
      }

      dom.devicesContainer.appendChild(card);
    });
  }

  /**
   * Render Alerts Section with Acknowledge Button
   */
  function renderAlerts() {
    const alerts = state.data.alerts || [];
    dom.alertsList.textContent = ''; // clear

    const unackedAlerts = alerts.filter((a) => !isAlertAcked(a));
    dom.unackCount.textContent = String(unackedAlerts.length);

    const filteredAlerts = state.alertFilter === 'unack' ? unackedAlerts : alerts;

    if (filteredAlerts.length === 0) {
      const emptyDiv = document.createElement('div');
      emptyDiv.className = 'empty-placeholder';

      const pTitle = document.createElement('p');
      pTitle.className = 'empty-title';
      pTitle.textContent = state.alertFilter === 'unack' ? 'Không có cảnh báo chưa xử lý' : 'Chưa có cảnh báo nào';

      const pDesc = document.createElement('p');
      pDesc.className = 'empty-desc';
      pDesc.textContent = 'Hiện tại không có cảnh báo nào chưa được xử lý. Lưu ý không có cảnh báo không đồng nghĩa an toàn khi chưa có ảnh chụp hoặc đánh giá thị giác.';

      emptyDiv.appendChild(pTitle);
      emptyDiv.appendChild(pDesc);
      dom.alertsList.appendChild(emptyDiv);
      return;
    }

    filteredAlerts.forEach((alert) => {
      const sev = translateSeverity(alert.severity);
      const isAcked = isAlertAcked(alert);
      const item = document.createElement('article');
      item.className = `alert-item ${sev.cardClass}`;
      if (isAcked) {
        item.classList.add('alert-item--acked');
      }

      // Header
      const header = document.createElement('div');
      header.className = 'alert-header';

      const titleWrap = document.createElement('div');
      titleWrap.className = 'alert-title-wrap';

      const sevBadge = document.createElement('span');
      sevBadge.className = `alert-severity-badge ${sev.className}`;
      sevBadge.textContent = sev.label;

      const timeEl = document.createElement('time');
      timeEl.className = 'alert-time';
      timeEl.textContent = formatDateTime(alert.created_at);

      titleWrap.appendChild(sevBadge);
      titleWrap.appendChild(timeEl);
      header.appendChild(titleWrap);
      item.appendChild(header);

      // Body: STRICT textContent to avoid XSS
      const body = document.createElement('p');
      body.className = 'alert-body';
      body.textContent = alert.message || 'Cảnh báo hệ thống không có mô tả chi tiết.';
      item.appendChild(body);

      // Footer
      const footer = document.createElement('div');
      footer.className = 'alert-footer';

      const devTag = document.createElement('span');
      devTag.className = 'alert-device-tag';
      devTag.textContent = alert.device_id || alert.printer_id ? `Nguồn: ${alert.device_id || alert.printer_id}` : 'Hệ thống';
      footer.appendChild(devTag);

      // Action: Ack button or Ack status
      if (isAcked) {
        const ackStatus = document.createElement('span');
        ackStatus.className = 'alert-ack-status';
        ackStatus.textContent = 'Đã xác nhận';
        footer.appendChild(ackStatus);
      } else {
        const ackBtn = document.createElement('button');
        ackBtn.type = 'button';
        ackBtn.className = 'btn btn-secondary btn-sm';
        ackBtn.textContent = 'Xác nhận đã xử lý';
        ackBtn.setAttribute('aria-label', `Xác nhận cảnh báo: ${alert.message || alert.id}`);

        ackBtn.addEventListener('click', async () => {
          ackBtn.disabled = true;
          ackBtn.textContent = 'Đang ghi nhận...';
          try {
            await ackAlert(alert.id);
            alert.acknowledged = 1;
            showToast('Đã xác nhận cảnh báo thành công', 'success');
            renderAlerts();
            renderSummaryMetrics();
          } catch (err) {
            ackBtn.disabled = false;
            ackBtn.textContent = 'Thử lại';
            showToast(`Lỗi xác nhận: ${err.message}`, 'error');
          }
        });

        footer.appendChild(ackBtn);
      }

      item.appendChild(footer);
      dom.alertsList.appendChild(item);
    });
  }

  /**
   * Render AI Analyses with Settings Diffs
   */
  function renderAnalyses() {
    const analyses = state.data.analyses || [];
    dom.analysesList.textContent = ''; // clear

    dom.analysesCountBadge.textContent = `${analyses.length} kết quả`;

    if (analyses.length === 0) {
      const emptyDiv = document.createElement('div');
      emptyDiv.className = 'empty-placeholder';

      const pTitle = document.createElement('p');
      pTitle.className = 'empty-title';
      pTitle.textContent = 'Chưa có phân tích AI nào';

      const pDesc = document.createElement('p');
      pDesc.className = 'empty-desc';
      pDesc.textContent = 'Khi người dùng kích hoạt tối ưu hóa từ Bambu Studio, tệp 3MF và đề xuất thay đổi thiết lập sẽ hiển thị tại đây.';

      emptyDiv.appendChild(pTitle);
      emptyDiv.appendChild(pDesc);
      dom.analysesList.appendChild(emptyDiv);
      return;
    }

    analyses.forEach((item) => {
      const card = document.createElement('article');
      card.className = 'analysis-card';

      // Header
      const header = document.createElement('div');
      header.className = 'analysis-header';

      const fileEl = document.createElement('h3');
      fileEl.className = 'analysis-file font-mono';
      fileEl.textContent = item.filename || `Dự án #${item.id || 'N/A'}`;

      const meta = document.createElement('div');
      meta.className = 'analysis-meta';

      const statusInfo = translateStatus(item.status);
      const statusBadge = document.createElement('span');
      statusBadge.className = `badge ${statusInfo.className}`;
      statusBadge.textContent = statusInfo.label;

      const objBadge = document.createElement('span');
      objBadge.className = 'badge badge-navy';
      objBadge.textContent = `Mục tiêu: ${translateObjective(item.objective)}`;

      const trigBadge = document.createElement('span');
      trigBadge.className = 'badge badge-muted';
      trigBadge.textContent = `Kích hoạt: ${translateTrigger(item.trigger)}`;

      const timeTag = document.createElement('span');
      timeTag.className = 'font-mono text-muted text-sm';
      timeTag.textContent = formatDateTime(item.created_at);

      meta.appendChild(statusBadge);
      meta.appendChild(objBadge);
      meta.appendChild(trigBadge);
      meta.appendChild(timeTag);

      header.appendChild(fileEl);
      header.appendChild(meta);
      card.appendChild(header);

      // AI Summary
      if (item.summary) {
        const sumBox = document.createElement('div');
        sumBox.className = 'analysis-summary-box';
        sumBox.textContent = item.summary;
        card.appendChild(sumBox);
      }

      // Settings Diffs Table (safe array or object map normalization)
      const parsedChanges = safeJsonParse(item.changes, item.changes);
      let normalizedChanges = [];
      if (Array.isArray(parsedChanges)) {
        normalizedChanges = parsedChanges;
      } else if (parsedChanges && typeof parsedChanges === 'object') {
        normalizedChanges = Object.entries(parsedChanges).map(([k, v]) => {
          if (v && typeof v === 'object' && !Array.isArray(v)) {
            return {
              key: k,
              old: v.old !== undefined ? v.old : '--',
              value: v.value !== undefined ? v.value : (v.new !== undefined ? v.new : JSON.stringify(v)),
              reason: v.reason || ''
            };
          }
          return {
            key: k,
            old: '--',
            value: String(v),
            reason: ''
          };
        });
      }

      if (normalizedChanges.length > 0) {
        const tableWrap = document.createElement('div');
        tableWrap.className = 'diffs-table-wrap';

        const table = document.createElement('table');
        table.className = 'diffs-table';

        // Head
        const thead = document.createElement('thead');
        const trHead = document.createElement('tr');
        ['Thông số thiết lập', 'Giá trị cũ', 'Đề xuất mới', 'Lý do từ AI'].forEach((headText) => {
          const th = document.createElement('th');
          th.textContent = headText;
          trHead.appendChild(th);
        });
        thead.appendChild(trHead);
        table.appendChild(thead);

        // Body
        const tbody = document.createElement('tbody');
        normalizedChanges.forEach((chg) => {
          const row = document.createElement('tr');

          // Key
          const tdKey = document.createElement('td');
          const spanKey = document.createElement('code');
          spanKey.className = 'diff-key';
          spanKey.textContent = chg.key || '--';
          tdKey.appendChild(spanKey);

          // Old value
          const tdOld = document.createElement('td');
          const spanOld = document.createElement('span');
          spanOld.className = 'diff-val-old';
          spanOld.textContent = chg.old !== undefined ? String(chg.old) : '--';
          tdOld.appendChild(spanOld);

          // New value
          const tdNew = document.createElement('td');
          const spanNew = document.createElement('span');
          spanNew.className = 'diff-val-new';
          spanNew.textContent = chg.value !== undefined ? String(chg.value) : '--';
          tdNew.appendChild(spanNew);

          // Reason: Safe textContent
          const tdReason = document.createElement('td');
          tdReason.className = 'diff-reason';
          tdReason.textContent = chg.reason || 'Tối ưu tự động';

          row.appendChild(tdKey);
          row.appendChild(tdOld);
          row.appendChild(tdNew);
          row.appendChild(tdReason);
          tbody.appendChild(row);
        });
        table.appendChild(tbody);
        tableWrap.appendChild(table);
        card.appendChild(tableWrap);
      }

      // Warnings (safe array parse from JSON string if needed)
      const warnings = safeJsonParse(item.warnings, []);
      if (Array.isArray(warnings) && warnings.length > 0) {
        const warnBox = document.createElement('div');
        warnBox.className = 'warnings-box';

        const warnHead = document.createElement('div');
        warnHead.className = 'warnings-header';
        warnHead.textContent = 'Cảnh báo an toàn từ mô hình AI:';
        warnBox.appendChild(warnHead);

        const ul = document.createElement('ul');
        ul.className = 'warnings-list';
        warnings.forEach((warn) => {
          const li = document.createElement('li');
          li.textContent = warn;
          ul.appendChild(li);
        });
        warnBox.appendChild(ul);
        card.appendChild(warnBox);
      }

      // Model metadata
      const modelObj = safeJsonParse(item.model || item.model_info, null);
      if (modelObj) {
        const modelFooter = document.createElement('div');
        modelFooter.className = 'text-dim text-sm font-mono';
        const modelName = typeof modelObj === 'string' ? modelObj : modelObj.name || JSON.stringify(modelObj);
        modelFooter.textContent = `Mô hình: ${modelName}`;
        card.appendChild(modelFooter);
      }

      dom.analysesList.appendChild(card);
    });
  }

  /**
   * Master sync runner
   */
  async function syncDashboard(manual = false) {
    if (state.isSyncing) return;
    if (!isAuthenticated()) {
      openLoginModal();
      return;
    }

    state.isSyncing = true;
    updateSyncIndicator('syncing', 'Đang đồng bộ...');

    try {
      const data = await fetchState();
      state.data = {
        analyses: Array.isArray(data.analyses) ? data.analyses : [],
        snapshots: Array.isArray(data.snapshots) ? data.snapshots : [],
        alerts: Array.isArray(data.alerts) ? data.alerts : [],
        devices: Array.isArray(data.devices) ? data.devices : [],
        printers: Array.isArray(data.printers) ? data.printers : [],
        config: data.config || {}
      };

      state.lastSyncTime = new Date();
      hideBanner();
      updateSyncIndicator('ok', `Cập nhật: ${formatTime(state.lastSyncTime)}`);

      // Revoke previous blobs before creating new ones
      revokeAllBlobUrls();

      // Render components
      renderConfigPills(state.data.config);
      renderSummaryMetrics();
      renderPrinters();
      await renderCameraSection();
      renderDevices();
      renderAlerts();
      renderAnalyses();

      if (manual) {
        showToast('Dữ liệu đã được cập nhật mới nhất', 'success');
      }
    } catch (err) {
      console.error('Lỗi khi đồng bộ dữ liệu dashboard:', err);
      updateSyncIndicator('error', 'Lỗi đồng bộ');

      if (err.message === 'UNAUTHORIZED' || err.message === 'NO_TOKEN') {
        showBanner('Mã xác thực không hợp lệ hoặc phiên làm việc đã hết hạn. Vui lòng xác thực lại.', true);
        openLoginModal('Mã xác thực không hợp lệ hoặc đã hết hạn. Vui lòng nhập mã truy cập.');
      } else {
        showBanner(`Không thể kết nối đến máy chủ PrintPilot (${err.message}). Vui lòng kiểm tra kết nối mạng nội bộ.`, true);
      }
    } finally {
      state.isSyncing = false;
    }
  }

  // --- Auto Refresh Controller ---

  function scheduleAutoRefresh() {
    if (state.refreshTimer) {
      clearInterval(state.refreshTimer);
      state.refreshTimer = null;
    }

    if (state.refreshIntervalMs > 0) {
      state.refreshTimer = setInterval(() => {
        syncDashboard(false);
      }, state.refreshIntervalMs);
    }
  }

  // --- Modal Controllers ---

  function openLoginModal(errorMessage = '') {
    dom.loginModal.style.display = 'flex';
    dom.tokenInput.value = state.token;
    if (errorMessage) {
      dom.loginError.style.display = 'block';
      dom.loginError.textContent = errorMessage;
    } else {
      dom.loginError.style.display = 'none';
    }
    setTimeout(() => dom.tokenInput.focus(), 50);
  }

  function closeLoginModal() {
    dom.loginModal.style.display = 'none';
    dom.loginError.style.display = 'none';
  }

  async function openLightboxModal() {
    const snapshots = visibleSnapshots();
    const current = snapshots.find((s) => s.id === state.selectedSnapshotId) || snapshots[0];
    if (!current) return;

    dom.lightboxModal.style.display = 'flex';
    dom.lightboxSubtitle.textContent = `Chụp lúc: ${formatDateTime(current.captured_at || current.created_at)} | Cầu nối: ${current.device_id || 'redmi-9c'}`;

    // Load full resolution blob
    try {
      const blobUrl = await fetchAuthenticatedImageBlob(current.id);
      dom.lightboxImg.src = blobUrl;
    } catch (err) {
      console.warn('Lỗi khi tải ảnh lightbox:', err);
    }

    // Telemetry grid
    dom.lightboxTelemetry.textContent = '';
    const tele = safeJsonParse(current.telemetry, {});
    const entries = Object.entries(tele);
    if (entries.length === 0) {
      const p = document.createElement('p');
      p.className = 'text-dim text-sm';
      p.textContent = 'Không có thông số đo từ xa kèm theo ảnh này.';
      dom.lightboxTelemetry.appendChild(p);
    } else {
      entries.forEach(([k, v]) => {
        const item = document.createElement('div');
        item.className = 'device-metric';

        const lbl = document.createElement('span');
        lbl.className = 'device-metric-lbl font-mono';
        lbl.textContent = k;

        const val = document.createElement('span');
        val.className = 'device-metric-val font-mono';
        val.textContent = typeof v === 'object' ? JSON.stringify(v) : String(v);

        item.appendChild(lbl);
        item.appendChild(val);
        dom.lightboxTelemetry.appendChild(item);
      });
    }

    dom.btnCloseLightbox.focus();
  }

  function closeLightboxModal() {
    dom.lightboxModal.style.display = 'none';
    dom.lightboxImg.src = '';
  }

  // --- Event Listeners Initialization ---

  function initEventListeners() {
    // Refresh controls
    dom.btnManualRefresh.addEventListener('click', () => {
      syncDashboard(true);
    });

    dom.autoRefreshSelect.value = String(state.refreshIntervalMs);
    dom.autoRefreshSelect.addEventListener('change', (e) => {
      state.refreshIntervalMs = parseInt(e.target.value, 10);
      sessionStorage.setItem(STORAGE_KEY_REFRESH_INTERVAL, String(state.refreshIntervalMs));
      scheduleAutoRefresh();
      if (state.refreshIntervalMs > 0) {
        showToast(`Đã đổi tần suất cập nhật sang ${e.target.selectedOptions[0].text}`, 'info');
      } else {
        showToast('Đã tạm dừng tự động làm mới', 'info');
      }
    });

    // Auth & Login
    dom.btnShowLogin.addEventListener('click', () => openLoginModal());
    dom.btnCancelLogin.addEventListener('click', () => closeLoginModal());

    dom.btnLogout.addEventListener('click', () => {
      if (confirm('Bạn có chắc chắn muốn đăng xuất?')) {
        sessionStorage.removeItem(STORAGE_KEY_TOKEN);
        state.token = '';
        revokeAllBlobUrls();
        updateAuthUI();
        openLoginModal('Đã đăng xuất. Vui lòng nhập mã truy cập để tiếp tục.');
      }
    });

    // Password visibility toggle
    dom.btnToggleToken.addEventListener('click', () => {
      const isPwd = dom.tokenInput.type === 'password';
      dom.tokenInput.type = isPwd ? 'text' : 'password';
      dom.btnToggleToken.setAttribute('aria-label', isPwd ? 'Ẩn mã token' : 'Hiển thị mã token');
    });

    // Login Form Submit
    dom.loginForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const tokenVal = dom.tokenInput.value.trim();

      if (!tokenVal) {
        dom.loginError.style.display = 'block';
        dom.loginError.textContent = 'Vui lòng nhập mã truy cập.';
        return;
      }

      dom.btnSubmitLogin.disabled = true;
      dom.loginSpinner.style.display = 'inline-block';
      dom.loginError.style.display = 'none';

      // Test token with /api/state
      const oldToken = state.token;
      state.token = tokenVal;

      try {
        await fetchState();
        // Success! Save to sessionStorage
        sessionStorage.setItem(STORAGE_KEY_TOKEN, tokenVal);
        updateAuthUI();
        closeLoginModal();
        showToast('Đăng nhập thành công', 'success');
        syncDashboard(false);
      } catch (err) {
        state.token = oldToken;
        dom.loginError.style.display = 'block';
        if (err.message === 'UNAUTHORIZED') {
          dom.loginError.textContent = 'Mã xác thực không đúng hoặc không có quyền truy cập.';
        } else {
          dom.loginError.textContent = `Không thể kết nối đến máy chủ: ${err.message}`;
        }
      } finally {
        dom.btnSubmitLogin.disabled = false;
        dom.loginSpinner.style.display = 'none';
      }
    });

    // Banner retry
    dom.bannerRetryBtn.addEventListener('click', () => {
      syncDashboard(true);
    });

    // Lightbox triggers
    dom.btnExpandSnapshot.addEventListener('click', () => openLightboxModal());
    dom.btnCloseLightbox.addEventListener('click', () => closeLightboxModal());

    // Alerts Filter Tabs
    dom.filterTabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        dom.filterTabs.forEach((t) => {
          t.classList.remove('active');
          t.setAttribute('aria-selected', 'false');
        });
        tab.classList.add('active');
        tab.setAttribute('aria-selected', 'true');
        state.alertFilter = tab.dataset.filter;
        renderAlerts();
      });
    });

    // Mobile nav scrolling
    dom.mobileNavBtns.forEach((btn) => {
      btn.addEventListener('click', () => {
        dom.mobileNavBtns.forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        const targetId = btn.dataset.target;
        const targetEl = document.getElementById(targetId);
        if (targetEl) {
          targetEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });
    });

    // Global Keyboard Shortcuts
    window.addEventListener('keydown', (e) => {
      // Escape closes modals
      if (e.key === 'Escape') {
        if (dom.lightboxModal.style.display !== 'none') {
          closeLightboxModal();
        } else if (dom.loginModal.style.display !== 'none') {
          closeLoginModal();
        }
      }

      // 'r' or 'R' refreshes if not typing in an input
      if ((e.key === 'r' || e.key === 'R') && !['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) {
        e.preventDefault();
        syncDashboard(true);
      }
    });

    // Update footer local time every second
    setInterval(() => {
      if (dom.footerClientTime) {
        dom.footerClientTime.textContent = `Thời gian cục bộ: ${formatDateTime(new Date().toISOString())}`;
      }
    }, 1000);
  }

  // --- Bootstrap Application ---
  async function detectTailscaleUser() {
    try {
      const res = await fetch('/api/whoami', { headers: { 'X-PrintPilot-Request': '1' }, cache: 'no-store' });
      if (!res.ok) return;
      const who = await res.json();
      if (who && who.authenticated && who.method === 'tailscale' && typeof who.user === 'string') {
        state.tsUser = who.user;
      }
    } catch (_) {
      // Offline or older server: fall back to token login
    }
  }

  async function init() {
    initEventListeners();
    await detectTailscaleUser();
    updateAuthUI();

    if (!isAuthenticated()) {
      openLoginModal();
    } else {
      syncDashboard(false);
      scheduleAutoRefresh();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
