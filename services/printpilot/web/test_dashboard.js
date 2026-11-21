/**
 * Real DOM Browser Simulation Test Suite for PrintPilot Dashboard
 *
 * Replaces implementation-mirroring string tests with an actual DOM execution test
 * that parses index.html, mocks standard browser APIs & PrintPilot backend API,
 * executes app.js, and exercises realistic user interactions and data handling.
 *
 * Verifies all review findings:
 * 1. Printer is Bambu A1; bridge is Redmi 9C (NEVER Redmi A1).
 * 2. Neutral copy: "Không có cảnh báo" / "Chưa có đánh giá" replaces "An toàn" / "hoạt động an toàn".
 * 3. Token fragments NEVER exposed in header.
 * 4. Implementation details (Bearer Token, sessionStorage, Tailscale) removed from product copy.
 * 5. Handles snapshot vision_result object, device last_status/last_error, analyses changes arrays/objects.
 * 6. Authentication API base override removed/restricted to same-origin.
 * 7. No instructions requiring LAN Only mode.
 * 8. Alert acknowledgment updates DOM and makes authenticated POST /api/alerts/{id}/ack.
 */

const fs = require('fs');
const path = require('path');
const assert = require('assert');
const vm = require('vm');

const WEB_DIR = __dirname;
const HTML_PATH = path.join(WEB_DIR, 'index.html');
const JS_PATH = path.join(WEB_DIR, 'app.js');

console.log('=== PrintPilot Web Dashboard DOM & API Verification ===\n');

// 1. Build a lightweight HTML parser and simulated DOM
class DOMTokenList extends Set {
  add(token) { super.add(token); }
  remove(token) { super.delete(token); }
  contains(token) { return super.has(token); }
  toggle(token, force) {
    if (force !== undefined) {
      if (force) super.add(token);
      else super.delete(token);
      return force;
    }
    if (super.has(token)) { super.delete(token); return false; }
    super.add(token); return true;
  }
  toString() { return Array.from(this).join(' '); }
}

class DOMElement {
  constructor(tagName = 'div') {
    this.tagName = tagName.toUpperCase();
    this.id = '';
    this.attributes = new Map();
    this.listeners = new Map();
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.dataset = {};
    this._value = '';
    this.disabled = false;
    this.type = 'text';
    this.src = '';
    this._classList = new DOMTokenList();
    this._textContent = '';
  }

  get className() {
    return this._classList.toString();
  }

  set className(val) {
    this._classList.clear();
    if (val) {
      val.split(/\s+/).filter(Boolean).forEach(c => this._classList.add(c));
    }
  }

  get classList() {
    return this._classList;
  }

  get value() {
    return this._value;
  }

  set value(val) {
    this._value = String(val);
  }

  get selectedOptions() {
    return [{ value: this._value, text: `${this._value} giây` }];
  }

  get textContent() {
    if (this.children.length === 0) {
      return this._textContent;
    }
    return this.children.map(c => c.textContent).join('');
  }

  set textContent(text) {
    this.children = [];
    this._textContent = String(text);
  }

  setAttribute(name, val) {
    this.attributes.set(name.toLowerCase(), String(val));
    if (name.toLowerCase() === 'id') this.id = String(val);
    if (name.toLowerCase() === 'class') this.className = String(val);
    if (name.toLowerCase() === 'type') this.type = String(val);
    if (name.toLowerCase().startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, g) => g.toUpperCase());
      this.dataset[key] = String(val);
    }
  }

  getAttribute(name) {
    if (name.toLowerCase() === 'id') return this.id;
    if (name.toLowerCase() === 'class') return this.className;
    if (name.toLowerCase() === 'type') return this.type;
    return this.attributes.get(name.toLowerCase()) || null;
  }

  removeAttribute(name) {
    this.attributes.delete(name.toLowerCase());
  }

  hasAttribute(name) {
    return this.attributes.has(name.toLowerCase());
  }

  appendChild(child) {
    if (child.parentNode) {
      child.parentNode.removeChild(child);
    }
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx !== -1) {
      this.children.splice(idx, 1);
      child.parentNode = null;
    }
    return child;
  }

  addEventListener(type, cb) {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, []);
    }
    this.listeners.get(type).push(cb);
  }

  removeEventListener(type, cb) {
    if (this.listeners.has(type)) {
      const list = this.listeners.get(type).filter(fn => fn !== cb);
      this.listeners.set(type, list);
    }
  }

  async dispatchEvent(event) {
    const ev = event || { type: 'custom', preventDefault() {} };
    if (!ev.target) ev.target = this;
    if (!ev.preventDefault) ev.preventDefault = () => {};
    const handlers = this.listeners.get(ev.type) || [];
    for (const h of handlers) {
      await h(ev);
    }
  }

  async click() {
    await this.dispatchEvent({ type: 'click', target: this, preventDefault() {} });
  }

  focus() {}
  scrollIntoView() {}

  getElementById(id) {
    if (this.id === id) return this;
    for (const c of this.children) {
      const match = c.getElementById(id);
      if (match) return match;
    }
    return null;
  }

  getElementsByTagName(name) {
    const res = [];
    const target = name.toUpperCase();
    for (const c of this.children) {
      if (target === '*' || c.tagName === target) res.push(c);
      res.push(...c.getElementsByTagName(name));
    }
    return res;
  }

  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }

  querySelectorAll(sel) {
    const res = [];
    const check = (el) => {
      if (sel.startsWith('#') && el.id === sel.slice(1)) return true;
      if (sel.startsWith('.') && el.classList.contains(sel.slice(1))) return true;
      if (el.tagName.toLowerCase() === sel.toLowerCase()) return true;
      return false;
    };
    const walk = (el) => {
      for (const c of el.children) {
        if (check(c)) res.push(c);
        walk(c);
      }
    };
    walk(this);
    return res;
  }
}

// 2. Parse HTML file into Simulated Document
function parseHtmlToDocument(htmlText) {
  const root = new DOMElement('HTML');
  const allElements = [];
  const tagRegex = /<([a-zA-Z0-9\-]+)([^>]*?)(\/?)>|([^<]+)|<\/([a-zA-Z0-9\-]+)>/g;
  let currentParent = root;
  const stack = [root];

  let match;
  while ((match = tagRegex.exec(htmlText)) !== null) {
    const [full, openTag, attrs, isSelfClosing, textContent, closeTag] = match;

    if (openTag) {
      const el = new DOMElement(openTag);
      const isSelf = Boolean(isSelfClosing) || ['meta', 'link', 'img', 'br', 'hr', 'input'].includes(openTag.toLowerCase());

      // Parse attributes
      const attrRegex = /([a-zA-Z0-9\-:_]+)(?:=(?:"([^"]*)"|'([^']*)'|([^>\s]+)))?/g;
      let aMatch;
      while ((aMatch = attrRegex.exec(attrs)) !== null) {
        const aName = aMatch[1];
        const aVal = aMatch[2] !== undefined ? aMatch[2] : (aMatch[3] !== undefined ? aMatch[3] : (aMatch[4] || ''));
        el.setAttribute(aName, aVal);
      }

      currentParent.appendChild(el);
      allElements.push(el);

      if (!isSelf) {
        stack.push(el);
        currentParent = el;
      }
    } else if (closeTag) {
      if (stack.length > 1 && stack[stack.length - 1].tagName.toLowerCase() === closeTag.toLowerCase()) {
        stack.pop();
        currentParent = stack[stack.length - 1];
      }
    } else if (textContent && textContent.trim()) {
      currentParent._textContent += textContent;
    }
  }

  const document = {
    readyState: 'complete',
    documentElement: root,
    body: root.getElementsByTagName('body')[0] || root,
    createElement: (tag) => new DOMElement(tag),
    getElementById: (id) => root.getElementById(id),
    querySelector: (sel) => root.querySelector(sel),
    querySelectorAll: (sel) => root.querySelectorAll(sel),
    getElementsByTagName: (tag) => root.getElementsByTagName(tag),
    addEventListener: (type, cb) => root.addEventListener(type, cb),
    removeEventListener: (type, cb) => root.removeEventListener(type, cb),
    dispatchEvent: (ev) => root.dispatchEvent(ev)
  };

  return document;
}

// 3. Mock Browser Global Environment
function createMockWindow(document, options = {}) {
  const sessionStorageMap = new Map();
  if (options.initialToken) {
    sessionStorageMap.set('printpilot_token', options.initialToken);
  }

  const mockFetchHistory = [];
  let blobCounter = 1;

  const mockWindow = {
    document,
    sessionStorage: {
      getItem: (k) => (sessionStorageMap.has(k) ? sessionStorageMap.get(k) : null),
      setItem: (k, v) => sessionStorageMap.set(k, String(v)),
      removeItem: (k) => sessionStorageMap.delete(k),
      clear: () => sessionStorageMap.clear()
    },
    URL: {
      createObjectURL: (blob) => `blob:http://localhost/test-blob-${blobCounter++}`,
      revokeObjectURL: () => {}
    },
    Headers: class {
      constructor(init = {}) {
        this.map = new Map();
        if (init instanceof Map) {
          init.forEach((v, k) => this.set(k, v));
        } else if (typeof init === 'object') {
          Object.entries(init).forEach(([k, v]) => this.set(k, v));
        }
      }
      set(k, v) { this.map.set(k.toLowerCase(), String(v)); }
      get(k) { return this.map.get(k.toLowerCase()) || null; }
      has(k) { return this.map.has(k.toLowerCase()); }
    },
    fetch: async (url, opts = {}) => {
      mockFetchHistory.push({ url, options: opts });
      return options.mockFetchHandler ? await options.mockFetchHandler(url, opts) : { ok: true, json: async () => ({}) };
    },
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
    setInterval: (fn, ms) => setInterval(fn, ms),
    clearInterval: (id) => clearInterval(id),
    addEventListener: (type, cb) => document.addEventListener(type, cb),
    confirm: () => true,
    console: {
      log: () => {},
      warn: () => {},
      error: () => {},
      info: () => {}
    }
  };

  mockWindow.window = mockWindow;
  return { mockWindow, mockFetchHistory };
}

// 4. Test Runner
async function runTests() {
  const htmlContent = fs.readFileSync(HTML_PATH, 'utf8');
  const jsContent = fs.readFileSync(JS_PATH, 'utf8');

  // Test 1: Static product copy & layout checks
  console.log('1. Kiểm tra văn bản sản phẩm (Product Copy & Brand Safety)...');
  assert(!htmlContent.includes('Redmi A1'), 'LỖI: index.html không được chứa "Redmi A1" (phải là Redmi 9C)');
  assert(!jsContent.includes('Redmi A1') && !jsContent.includes('redmi-a1'), 'LỖI: app.js không được chứa "Redmi A1" hoặc "redmi-a1"');
  assert(htmlContent.includes('Redmi 9C'), 'index.html phải hiển thị cầu nối Redmi 9C');
  assert(htmlContent.includes('Bambu A1'), 'index.html phải hiển thị máy in Bambu A1');

  // Neutral alert language check
  assert(!htmlContent.includes('>An toàn<'), 'LỖI: index.html không được dùng nhãn "An toàn" cho trạng thái không có cảnh báo');
  assert(!htmlContent.includes('hệ thống hoạt động an toàn ổn định'), 'LỖI: không được khẳng định hệ thống an toàn ổn định khi chưa có đánh giá');
  assert(!jsContent.includes("'An toàn'"), 'LỖI: app.js không được gán textContent là "An toàn" khi không có cảnh báo');

  // Implementation details removed from copy
  assert(!htmlContent.includes('Bearer Token'), 'LỖI: index.html không được để lộ thuật ngữ triển khai "Bearer Token" trong giao diện người dùng');
  assert(!htmlContent.includes('Tailscale Private Node'), 'LỖI: index.html không được hiển thị "Tailscale" trong giao diện chung');
  assert(!htmlContent.includes('sessionStorage'), 'LỖI: index.html không được để lộ "sessionStorage" trong tooltip/nội dung giao diện');

  // LAN Only removal check: never instruct or require LAN Only mode
  assert(!htmlContent.includes('yêu cầu chế độ LAN') && !htmlContent.includes('bắt buộc chế độ LAN') && !htmlContent.includes('yêu cầu LAN Only'), 'LỖI: index.html không được yêu cầu chế độ LAN Only');
  assert(htmlContent.includes('Bambu Handy'), 'index.html nên nêu rõ máy in duy trì kết nối đám mây cho Bambu Handy');
  console.log('  ✓ Toàn bộ nhãn, thương hiệu, thuật ngữ triển khai và khuyến nghị LAN Only hợp lệ!');

  // Test 2: Execution in real DOM with mocked API
  console.log('2. Khởi chạy ứng dụng app.js trên môi trường DOM mô phỏng...');

  const document = parseHtmlToDocument(htmlContent);

  // Mock API Responses
  const mockStateData = {
    analyses: [
      {
        id: 'ana-101',
        created_at: new Date().toISOString(),
        filename: 'gear_bracket.3mf',
        status: 'complete',
        objective: 'quality',
        trigger: 'send',
        summary: 'Tối ưu độ chính xác bề mặt răng bánh răng',
        changes: [
          { key: 'layer_height', old: 0.2, value: 0.12, reason: 'Giảm bậc thang bề mặt nghiêng' }
        ],
        warnings: ['Tăng thời gian in 15 phút'],
        model: { name: 'gemini-1.5-pro' }
      },
      {
        id: 'ana-102',
        created_at: new Date().toISOString(),
        filename: 'box_cover.3mf',
        status: 'complete',
        objective: 'speed',
        trigger: 'manual',
        summary: 'Tối ưu tốc độ di chuyển',
        // Changes as an Object map (testing finding 5)
        changes: {
          travel_speed: { old: 200, value: 300, reason: 'Tăng tốc di chuyển không tải' }
        },
        warnings: [],
        model: 'gemini-1.5-flash'
      }
    ],
    snapshots: [
      {
        id: 'snap-888',
        device_id: 'redmi-9c-cam',
        printer_id: 'bambu-a1-main',
        captured_at: new Date(Date.now() - 20000).toISOString(), // 20s ago (fresh)
        created_at: new Date().toISOString(),
        status: 'printing',
        telemetry: {
          nozzle_temp: 215,
          bed_temp: 60,
          progress: 58,
          current_layer: 116,
          total_layers: 200,
          fan_speed: 100
        },
        // vision_result as an Object (testing finding 5)
        vision_result: {
          summary: 'Bề mặt in mịn, không có sợi nhựa thừa hay cong vênh.',
          confidence: 0.99,
          status: 'clear'
        }
      }
    ],
    alerts: [
      {
        id: 'alt-555',
        created_at: new Date().toISOString(),
        severity: 'warning',
        message: 'Nhiệt độ đầu in dao động 5 độ C so với mục tiêu',
        device_id: 'bambu-a1-main',
        acknowledged: false
      }
    ],
    devices: [
      {
        device_id: 'redmi-9c-cam',
        printer_id: 'bambu-a1-main',
        last_seen: new Date().toISOString(),
        last_status: 'printing',
        last_error: null
      },
      {
        device_id: 'redmi-9c-aux',
        printer_id: 'bambu-a1-aux',
        last_seen: new Date(Date.now() - 3600000).toISOString(),
        last_status: 'error',
        last_error: 'Không thể kết nối đến camera TLS port 6000'
      }
    ],
    config: {
      ai_configured: true,
      vision_configured: true,
      notifications_configured: false
    }
  };

  let ackAlertCalledWith = null;

  const mockFetchHandler = async (url, opts) => {
    // Strictly verify relative same-origin URL
    assert(url.startsWith('/api/'), `LỖI: Request URL phải là cùng nguồn relative (/api/...), nhận được: "${url}"`);

    // Verify Bearer token authorization header
    const headers = opts.headers;
    const authHeader = headers && (headers.get ? headers.get('Authorization') : headers['Authorization']);
    assert(authHeader && authHeader.startsWith('Bearer '), 'Mọi request API phải có Bearer token hợp lệ');

    if (url === '/api/state') {
      return {
        ok: true,
        status: 200,
        json: async () => JSON.parse(JSON.stringify(mockStateData))
      };
    }

    if (url.startsWith('/api/images/')) {
      return {
        ok: true,
        status: 200,
        blob: async () => ({ type: 'image/jpeg', size: 1024 })
      };
    }

    if (url === '/api/alerts/alt-555/ack') {
      ackAlertCalledWith = url;
      return {
        ok: true,
        status: 200,
        json: async () => ({ acknowledged: true })
      };
    }

    throw new Error(`Endpoint không hỗ trợ trong mock: ${url}`);
  };

  const { mockWindow, mockFetchHistory } = createMockWindow(document, {
    initialToken: '',
    mockFetchHandler
  });

  // Execute app.js in simulated VM sandbox
  const context = vm.createContext({
    ...mockWindow,
    document,
    window: mockWindow,
    Headers: mockWindow.Headers,
    URL: mockWindow.URL,
    sessionStorage: mockWindow.sessionStorage,
    fetch: mockWindow.fetch,
    setTimeout: mockWindow.setTimeout,
    clearTimeout: mockWindow.clearTimeout,
    setInterval: mockWindow.setInterval,
    clearInterval: mockWindow.clearInterval,
    confirm: mockWindow.confirm
  });

  vm.runInContext(jsContent, context);

  // Initial state check: no token -> login modal should be shown
  const loginModal = document.getElementById('login-modal');
  assert(loginModal.style.display !== 'none', 'Khi chưa có token, modal đăng nhập phải hiển thị');

  // Test 3: Log in with Token
  console.log('3. Kiểm tra đăng nhập với Access Token và bảo mật Header...');
  const tokenInput = document.getElementById('token-input');
  const loginForm = document.getElementById('login-form');
  const TEST_SECRET_TOKEN = 'secret-manager-token-abc-9876543210';

  tokenInput.value = TEST_SECRET_TOKEN;
  await loginForm.dispatchEvent({ type: 'submit', preventDefault() {} });
  // Wait for async syncDashboard to complete rendering
  await new Promise(r => setTimeout(r, 50));

  // Modal must be closed after successful login
  assert.strictEqual(loginModal.style.display, 'none', 'Modal đăng nhập phải ẩn sau khi xác thực thành công');
  assert.strictEqual(mockWindow.sessionStorage.getItem('printpilot_token'), TEST_SECRET_TOKEN, 'Token phải được lưu vào sessionStorage');

  // Verify Header does NOT expose token fragments
  const authTokenPreview = document.getElementById('auth-token-preview');
  const headerPreviewText = authTokenPreview.textContent;
  assert(!headerPreviewText.includes('secret'), 'LỖI: Header không được để lộ đoạn token bí mật');
  assert(!headerPreviewText.includes('9876543210'), 'LỖI: Header không được để lộ đuôi token');
  assert.strictEqual(headerPreviewText, 'Đã xác thực', 'Header phải hiển thị nhãn trung tính "Đã xác thực" thay vì cắt chuỗi token');
  console.log(`  ✓ Header hiển thị bảo mật: "${headerPreviewText}" (Không rò rỉ token fragment)`);

  // Test 4: Verify Camera HUD & Vision Assessment Object Handling
  console.log('4. Kiểm tra hiển thị ảnh Camera và Vision AI (Object support)...');
  const hudDeviceId = document.getElementById('hud-device-id');
  assert(hudDeviceId.textContent.includes('redmi-9c-cam'), 'Camera HUD phải hiển thị thiết bị redmi-9c-cam');
  assert(!hudDeviceId.textContent.includes('redmi-a1'), 'LỖI: Camera HUD không được hiển thị redmi-a1');

  const visionBox = document.getElementById('vision-assessment-box');
  const visionText = document.getElementById('vision-assessment-text');
  assert(visionBox.style.display !== 'none', 'Hộp đánh giá thị giác phải hiển thị khi có vision_result');
  assert(!visionText.textContent.includes('[object Object]'), 'LỖI: vision_result object không được hiển thị dưới dạng "[object Object]"');
  assert(visionText.textContent.includes('Bề mặt in mịn'), 'vision_result phải hiển thị tóm tắt từ đối tượng JSON');
  console.log(`  ✓ Đánh giá thị giác đối tượng hợp lệ: "${visionText.textContent}"`);

  // Test 5: Verify Devices section with last_status & last_error
  console.log('5. Kiểm tra hiển thị danh sách thiết bị, last_status & last_error...');
  const devicesContainer = document.getElementById('devices-container');
  const deviceCards = devicesContainer.querySelectorAll('.device-card');
  assert.strictEqual(deviceCards.length, 2, 'Phải hiển thị đủ 2 thiết bị');

  const containerText = devicesContainer.textContent;
  assert(containerText.includes('redmi-9c-cam') && containerText.includes('redmi-9c-aux'), 'Phải chứa mã thiết bị Redmi 9C');
  assert(!containerText.includes('Redmi A1'), 'LỖI: Không được chứa Redmi A1');
  assert(containerText.includes('Lỗi thiết bị: Không thể kết nối đến camera TLS port 6000'), 'Phải hiển thị last_error của thiết bị lỗi');
  console.log('  ✓ Danh sách thiết bị và trạng thái lỗi last_error hiển thị chính xác!');

  // Test 6: Verify Analyses section with changes Array and Object
  console.log('6. Kiểm tra hiển thị bảng thay đổi thông số AI (changes Array & Object)...');
  const analysesList = document.getElementById('analyses-list');
  const diffTables = analysesList.querySelectorAll('.diffs-table');
  assert.strictEqual(diffTables.length, 2, 'Cả hai phân tích (Array và Object) đều phải dựng được bảng diffs');

  const analysesText = analysesList.textContent;
  assert(analysesText.includes('layer_height') && analysesText.includes('0.12'), 'Phải hiển thị thay đổi từ mảng changes');
  assert(analysesText.includes('travel_speed') && analysesText.includes('300'), 'Phải hiển thị thay đổi từ đối tượng changes');
  console.log('  ✓ Phân tích AI và chuẩn hóa mảng/đối tượng diffs hoàn hảo!');

  // Test 7: Verify Alert Acknowledgment and Neutral Status
  console.log('7. Kiểm tra xử lý Cảnh báo và tính trung tính (Không dùng "An toàn")...');
  const statAlertsBadge = document.getElementById('stat-alerts-badge');
  const statAlertsCount = document.getElementById('stat-alerts-count');

  assert.strictEqual(statAlertsCount.textContent, '1', 'Số lượng cảnh báo ban đầu phải là 1');
  assert.strictEqual(statAlertsBadge.textContent, '1 cần xử lý', 'Badge cảnh báo phải hiển thị "1 cần xử lý"');

  // Find Ack button and click it
  const alertsList = document.getElementById('alerts-list');
  const ackBtn = alertsList.querySelector('button');
  assert(ackBtn, 'Phải có nút xác nhận cảnh báo');
  await ackBtn.click();
  await new Promise(r => setTimeout(r, 20));

  assert.strictEqual(ackAlertCalledWith, '/api/alerts/alt-555/ack', 'Nút xác nhận phải gọi đúng endpoint POST /api/alerts/{id}/ack');
  assert.strictEqual(statAlertsCount.textContent, '0', 'Sau khi xác nhận, số cảnh báo chưa xử lý phải về 0');
  assert.strictEqual(statAlertsBadge.textContent, 'Không có cảnh báo', 'Khi cảnh báo về 0, badge phải là "Không có cảnh báo" (NEVER "An toàn")');
  console.log(`  ✓ Trạng thái cảnh báo cập nhật chuẩn xác: "${statAlertsBadge.textContent}"`);

  // Test 8: Verify API base restriction (Same-origin enforcement)
  console.log('8. Kiểm tra tính toàn vẹn cùng nguồn (Same-origin Security)...');
  const apiRequests = mockFetchHistory.map(h => h.url);
  assert(apiRequests.every(url => url.startsWith('/api/')), 'Tất cả các API calls đều phải sử dụng đường dẫn cùng nguồn relative');
  console.log('  ✓ Không có bất kỳ credential nào bị gửi đến máy chủ lạ ngoài cùng nguồn!');

  console.log('\n====================================================');
  console.log('TẤT CẢ CÁC BÀI KIỂM THỬ TRÌNH DUYỆT THỰC TẾ ĐÃ HOÀN TẤT THÀNH CÔNG 100%!');
  process.exit(0);
}

runTests().catch(err => {
  console.error('\n✗ KIỂM THỬ THẤT BẠI:', err);
  process.exit(1);
});
