/* PrintPilot 3D preview: a tiny WebGL2 orbit viewer for the server's PPM1 mesh.
 * No dependencies (the dashboard CSP allows same-origin scripts only).
 *
 *   const v = PPViewer.create(canvas, arrayBuffer);  // throws if unsupported
 *   v.reset(); v.dispose();
 */
(function () {
  'use strict';

  function parse(buf) {
    const dv = new DataView(buf);
    if (buf.byteLength < 16 || dv.getUint32(0, false) !== 0x50504d31) throw new Error('bad mesh');
    const nv = dv.getUint32(4, true);
    const nt = dv.getUint32(8, true);
    const nc = dv.getUint32(12, true);
    const palOff = 16;
    const posOff = palOff + Math.ceil((nc * 3) / 4) * 4;
    const idxOff = posOff + nv * 12;
    const colOff = idxOff + nt * 12;
    if (colOff + nt > buf.byteLength) throw new Error('truncated mesh');
    const pal = new Uint8Array(buf, palOff, nc * 3);
    const pos = new Float32Array(buf, posOff, nv * 3);
    const idx = new Uint32Array(buf, idxOff, nt * 3);
    const tcol = new Uint8Array(buf, colOff, nt);

    // Expand to flat triangles: per-triangle colour, normals from derivatives.
    const P = new Float32Array(nt * 9);
    const C = new Uint8Array(nt * 9);
    const lo = [Infinity, Infinity, Infinity];
    const hi = [-Infinity, -Infinity, -Infinity];
    for (let t = 0; t < nt; t++) {
      const ci = Math.min(tcol[t], nc - 1) * 3;
      for (let k = 0; k < 3; k++) {
        const vi = idx[t * 3 + k];
        if (vi >= nv) throw new Error('bad index');
        const o = (t * 3 + k) * 3;
        for (let a = 0; a < 3; a++) {
          const v = pos[vi * 3 + a];
          P[o + a] = v;
          if (v < lo[a]) lo[a] = v;
          if (v > hi[a]) hi[a] = v;
          C[o + a] = pal[ci + a];
        }
      }
    }
    const center = [0, 1, 2].map((a) => (lo[a] + hi[a]) / 2);
    const radius = Math.max(1e-3, Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) / 2);
    return { P, C, count: nt * 3, center, radius, triangles: nt, size: [0, 1, 2].map((a) => hi[a] - lo[a]) };
  }

  // Column-major 4x4 helpers
  function perspective(fovy, aspect, near, far) {
    const f = 1 / Math.tan(fovy / 2);
    const nf = 1 / (near - far);
    return [f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0];
  }
  function lookAt(eye, at, up) {
    let z = [eye[0] - at[0], eye[1] - at[1], eye[2] - at[2]];
    let l = Math.hypot(...z); z = z.map((v) => v / l);
    let x = [up[1] * z[2] - up[2] * z[1], up[2] * z[0] - up[0] * z[2], up[0] * z[1] - up[1] * z[0]];
    l = Math.hypot(...x) || 1; x = x.map((v) => v / l);
    const y = [z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]];
    const d = (a) => -(a[0] * eye[0] + a[1] * eye[1] + a[2] * eye[2]);
    return [x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0, d(x), d(y), d(z), 1];
  }
  function mul(a, b) {
    const o = new Array(16);
    for (let c = 0; c < 4; c++) {
      for (let r = 0; r < 4; r++) {
        o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
      }
    }
    return o;
  }

  const VS = `#version 300 es
in vec3 aPos; in vec3 aCol;
uniform mat4 uMV; uniform mat4 uP;
out vec3 vCol; out vec3 vView;
void main() {
  vec4 v = uMV * vec4(aPos, 1.0);
  vView = v.xyz; vCol = aCol;
  gl_Position = uP * v;
}`;
  const FS = `#version 300 es
precision highp float;
in vec3 vCol; in vec3 vView; out vec4 outColor;
void main() {
  vec3 n = normalize(cross(dFdx(vView), dFdy(vView)));
  vec3 key = normalize(vec3(0.35, 0.55, 1.0));
  vec3 fill = normalize(vec3(-0.6, -0.2, 0.4));
  float light = 0.32 + 0.58 * max(dot(n, key), 0.0) + 0.18 * max(dot(n, fill), 0.0);
  outColor = vec4(vCol * min(light, 1.1), 1.0);
}`;

  function compile(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || 'shader');
    return s;
  }

  function create(canvas, buffer) {
    const mesh = parse(buffer);
    const gl = canvas.getContext('webgl2', { antialias: true, alpha: true, premultipliedAlpha: true });
    if (!gl) throw new Error('webgl2');
    const prog = gl.createProgram();
    gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VS));
    gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error('link');
    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    const bufs = [];
    function attr(name, data, type, normalized) {
      const b = gl.createBuffer();
      bufs.push(b);
      gl.bindBuffer(gl.ARRAY_BUFFER, b);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
      const loc = gl.getAttribLocation(prog, name);
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 3, type, normalized, 0, 0);
    }
    attr('aPos', mesh.P, gl.FLOAT, false);
    attr('aCol', mesh.C, gl.UNSIGNED_BYTE, true);
    const uMV = gl.getUniformLocation(prog, 'uMV');
    const uP = gl.getUniformLocation(prog, 'uP');

    const home = { yaw: -Math.PI / 2, pitch: 0.75, dist: mesh.radius * 3.1 };
    const cam = { ...home };
    let frame = 0;
    let disposed = false;

    function draw() {
      frame = 0;
      if (disposed) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
      const h = Math.max(1, Math.round(canvas.clientHeight * dpr));
      if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
      gl.viewport(0, 0, w, h);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.enable(gl.DEPTH_TEST);
      gl.useProgram(prog);
      const c = mesh.center;
      const cp = Math.cos(cam.pitch);
      const eye = [c[0] + cam.dist * cp * Math.cos(cam.yaw), c[1] + cam.dist * cp * Math.sin(cam.yaw), c[2] + cam.dist * Math.sin(cam.pitch)];
      const view = lookAt(eye, c, [0, 0, 1]);
      const proj = perspective(0.6, w / h, cam.dist / 100, cam.dist + mesh.radius * 4);
      gl.uniformMatrix4fv(uMV, false, view);
      gl.uniformMatrix4fv(uP, false, proj);
      gl.bindVertexArray(vao);
      gl.drawArrays(gl.TRIANGLES, 0, mesh.count);
    }
    function redraw() { if (!frame && !disposed) frame = requestAnimationFrame(draw); }

    // Orbit: drag rotates, wheel / pinch zooms, double-click resets
    const pointers = new Map();
    let pinch = 0;
    function zoom(factor) {
      cam.dist = Math.min(mesh.radius * 12, Math.max(mesh.radius * 0.6, cam.dist * factor));
      redraw();
    }
    function onDown(e) {
      canvas.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, [e.clientX, e.clientY]);
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinch = Math.hypot(a[0] - b[0], a[1] - b[1]);
      }
    }
    function onMove(e) {
      const prev = pointers.get(e.pointerId);
      if (!prev) return;
      pointers.set(e.pointerId, [e.clientX, e.clientY]);
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
        if (pinch > 0 && d > 0) zoom(pinch / d);
        pinch = d;
        return;
      }
      cam.yaw -= (e.clientX - prev[0]) * 0.01;
      cam.pitch = Math.max(-1.45, Math.min(1.45, cam.pitch + (e.clientY - prev[1]) * 0.01));
      redraw();
    }
    function onUp(e) { pointers.delete(e.pointerId); pinch = 0; }
    function onWheel(e) { e.preventDefault(); zoom(Math.exp(e.deltaY * 0.0015)); }
    function reset() { Object.assign(cam, home); redraw(); }
    function onKey(e) {
      const step = { ArrowLeft: [0.15, 0], ArrowRight: [-0.15, 0], ArrowUp: [0, 0.15], ArrowDown: [0, -0.15] }[e.key];
      if (step) {
        e.preventDefault();
        cam.yaw += step[0];
        cam.pitch = Math.max(-1.45, Math.min(1.45, cam.pitch + step[1]));
        redraw();
      } else if (e.key === '+' || e.key === '=') { zoom(0.85); } else if (e.key === '-') { zoom(1.18); } else if (e.key === '0') { reset(); }
    }
    canvas.addEventListener('pointerdown', onDown);
    canvas.addEventListener('pointermove', onMove);
    canvas.addEventListener('pointerup', onUp);
    canvas.addEventListener('pointercancel', onUp);
    canvas.addEventListener('wheel', onWheel, { passive: false });
    canvas.addEventListener('dblclick', reset);
    canvas.addEventListener('keydown', onKey);
    const ro = new ResizeObserver(redraw);
    ro.observe(canvas);
    canvas.addEventListener('webglcontextlost', (e) => e.preventDefault());
    redraw();

    return {
      triangles: mesh.triangles,
      size: mesh.size,
      reset,
      dispose() {
        if (disposed) return;
        disposed = true;
        if (frame) cancelAnimationFrame(frame);
        ro.disconnect();
        canvas.removeEventListener('wheel', onWheel);
        bufs.forEach((b) => gl.deleteBuffer(b));
        gl.deleteVertexArray(vao);
        gl.deleteProgram(prog);
        const lose = gl.getExtension('WEBGL_lose_context');
        if (lose) lose.loseContext();
      },
    };
  }

  window.PPViewer = { create, parse };
})();
