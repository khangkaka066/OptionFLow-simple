const VERTEX_SHADER = `
attribute vec3 aPosition;
attribute vec3 aColor;
uniform mat4 uMatrix;
varying vec3 vColor;
void main() {
  vColor = aColor;
  gl_Position = uMatrix * vec4(aPosition, 1.0);
}`;

const COLOR_SHADER = `
precision mediump float;
varying vec3 vColor;
void main() {
  gl_FragColor = vec4(vColor, 1.0);
}`;

const TEXT_VERTEX_SHADER = `
attribute vec3 aPosition;
attribute vec2 aTexcoord;
uniform mat4 uMatrix;
varying vec2 vTexcoord;
void main() {
  vTexcoord = aTexcoord;
  gl_Position = uMatrix * vec4(aPosition, 1.0);
}`;

const TEXT_FRAGMENT_SHADER = `
precision mediump float;
uniform sampler2D uTexture;
varying vec2 vTexcoord;
void main() {
  vec4 c = texture2D(uTexture, vTexcoord);
  if (c.a < 0.05) discard;
  gl_FragColor = c;
}`;

function clamp(value, min, max) { return Math.max(min, Math.min(max, value)); }
function lerp(a, b, t) { return a + (b - a) * t; }
function sub(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
function add(a, b) { return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]; }
function scale(a, s) { return [a[0] * s, a[1] * s, a[2] * s]; }
function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
function norm(a) { const len = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / len, a[1] / len, a[2] / len]; }

function perspective(fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2);
  const nf = 1 / (near - far);
  return [
    f / aspect, 0, 0, 0,
    0, f, 0, 0,
    0, 0, (far + near) * nf, -1,
    0, 0, (2 * far * near) * nf, 0,
  ];
}

function lookAt(eye, target, up) {
  const z = norm(sub(eye, target));
  const x = norm(cross(up, z));
  const y = cross(z, x);
  return [
    x[0], y[0], z[0], 0,
    x[1], y[1], z[1], 0,
    x[2], y[2], z[2], 0,
    -dot(x, eye), -dot(y, eye), -dot(z, eye), 1,
  ];
}

function multiply(a, b) {
  const out = new Array(16).fill(0);
  for (let row = 0; row < 4; row++) {
    for (let col = 0; col < 4; col++) {
      for (let i = 0; i < 4; i++) out[col * 4 + row] += a[i * 4 + row] * b[col * 4 + i];
    }
  }
  return out;
}

function compile(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
  return shader;
}

function program(gl, vertex, fragment) {
  const p = gl.createProgram();
  gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vertex));
  gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, fragment));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
  return p;
}

function colorFor(value) {
  const t = clamp(Math.abs(value), 0, 1);
  const zero = [0.07, 0.10, 0.16];
  const pos = [0.13, 0.83, 0.93];
  const neg = [0.49, 0.23, 0.93];
  const target = value >= 0 ? pos : neg;
  return [lerp(zero[0], target[0], t), lerp(zero[1], target[1], t), lerp(zero[2], target[2], t)];
}

function formatCompact(value) {
  const n = Number(value || 0);
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  if (abs >= 1e9) return sign + (abs / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return sign + (abs / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return sign + (abs / 1e3).toFixed(1) + "K";
  return sign + abs.toFixed(0);
}

export class Surface3DRenderer {
  constructor(container) {
    this.container = container;
    this.canvas = document.createElement("canvas");
    this.container.replaceChildren(this.canvas);
    this.gl = this.canvas.getContext("webgl", {antialias: true, alpha: false, preserveDrawingBuffer: true});
    if (!this.gl) throw new Error("WebGL is not available");
    this.colorProgram = program(this.gl, VERTEX_SHADER, COLOR_SHADER);
    this.textProgram = program(this.gl, TEXT_VERTEX_SHADER, TEXT_FRAGMENT_SHADER);
    this.mesh = null;
    this.lines = null;
    this.labels = [];
    this.defaultView = {radius: 4.6, azimuth: -0.75, polar: 1.05, target: [0, 0, 0]};
    this.view = {...this.defaultView, target: [...this.defaultView.target]};
    this.drag = null;
    this._bindEvents();
    this.resize();
  }

  destroy() {
    window.removeEventListener("resize", this._resizeHandler);
  }

  resetView() {
    this.view = {...this.defaultView, target: [...this.defaultView.target]};
    this.render();
  }

  setData(payload) {
    this.payload = payload;
    this._disposeLabels();
    const built = this._buildGeometry(payload);
    this.mesh = this._buffer(built.mesh, this.gl.TRIANGLES);
    this.lines = this._buffer(built.lines, this.gl.LINES);
    this.labels = built.labels.map(label => this._makeLabel(label.text, label.position, label.size || 0.075));
    this.render();
  }

  resize() {
    const rect = this.container.getBoundingClientRect();
    const ratio = Math.max(2, Math.min(2.4, window.devicePixelRatio || 1));
    const width = Math.max(2, Math.round(rect.width * ratio));
    const height = Math.max(2, Math.round(rect.height * ratio));
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
    this.render();
  }

  render() {
    const gl = this.gl;
    if (!gl) return;
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clearColor(0.008, 0.015, 0.032, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    const matrix = this._cameraMatrix();
    if (this.mesh) this._drawColorBuffer(this.mesh, matrix, gl.TRIANGLES);
    if (this.lines) this._drawColorBuffer(this.lines, matrix, gl.LINES);
    this._drawLabels(matrix);
  }

  _bindEvents() {
    this._resizeHandler = () => this.resize();
    window.addEventListener("resize", this._resizeHandler);
    this.canvas.addEventListener("wheel", event => {
      event.preventDefault();
      this.view.radius = clamp(this.view.radius * (1 + event.deltaY * 0.0012), 1.1, 12);
      this.render();
    }, {passive: false});
    this.canvas.addEventListener("pointerdown", event => {
      this.canvas.setPointerCapture(event.pointerId);
      this.container.classList.add("dragging");
      this.drag = {x: event.clientX, y: event.clientY, azimuth: this.view.azimuth, polar: this.view.polar};
    });
    this.canvas.addEventListener("pointermove", event => {
      if (!this.drag) return;
      const dx = event.clientX - this.drag.x;
      const dy = event.clientY - this.drag.y;
      this.view.azimuth = this.drag.azimuth - dx * 0.008;
      this.view.polar = clamp(this.drag.polar + dy * 0.006, 0.28, 1.48);
      this.render();
    });
    const end = event => {
      if (this.drag) {
        this.drag = null;
        this.container.classList.remove("dragging");
      }
      try { this.canvas.releasePointerCapture(event.pointerId); } catch (_err) {}
    };
    this.canvas.addEventListener("pointerup", end);
    this.canvas.addEventListener("pointercancel", end);
  }

  _cameraPosition() {
    const {radius, azimuth, polar, target} = this.view;
    return [
      target[0] + radius * Math.sin(polar) * Math.sin(azimuth),
      target[1] + radius * Math.cos(polar),
      target[2] + radius * Math.sin(polar) * Math.cos(azimuth),
    ];
  }

  _cameraMatrix() {
    const eye = this._cameraPosition();
    const view = lookAt(eye, this.view.target, [0, 1, 0]);
    const aspect = this.canvas.width / Math.max(1, this.canvas.height);
    const proj = perspective(Math.PI / 4, aspect, 0.05, 50);
    return multiply(proj, view);
  }

  _buildGeometry(payload) {
    const strikes = payload?.strikes || [];
    const expiries = payload?.expiries || [];
    const values = payload?.values || [];
    const rowCount = Math.max(1, expiries.length);
    const colCount = Math.max(1, strikes.length);
    const renderRows = rowCount === 1 ? 2 : rowCount;
    const xAt = col => colCount === 1 ? 0 : -1.75 + (col / (colCount - 1)) * 3.5;
    const minStrike = Number(strikes[0]);
    const maxStrike = Number(strikes[strikes.length - 1]);
    const xForStrike = strike => {
      if (!Number.isFinite(strike) || !Number.isFinite(minStrike) || !Number.isFinite(maxStrike) || minStrike === maxStrike) return null;
      if (strike < minStrike || strike > maxStrike) return null;
      return -1.75 + ((strike - minStrike) / (maxStrike - minStrike)) * 3.5;
    };
    const zAt = row => renderRows === 1 ? 0 : -1.05 + (row / (renderRows - 1)) * 2.1;
    const yAt = (row, col) => {
      const sourceRow = rowCount === 1 ? 0 : row;
      const v = Number(values[sourceRow]?.[col] || 0);
      return v * 0.78;
    };
    const vertex = (row, col) => {
      const v = Number(values[rowCount === 1 ? 0 : row]?.[col] || 0);
      return {pos: [xAt(col), yAt(row, col), zAt(row)], color: colorFor(v)};
    };
    const mesh = [];
    const pushVertex = v => mesh.push(...v.pos, ...v.color);
    for (let r = 0; r < renderRows - 1; r++) {
      for (let c = 0; c < colCount - 1; c++) {
        const a = vertex(r, c), b = vertex(r, c + 1), d = vertex(r + 1, c), e = vertex(r + 1, c + 1);
        pushVertex(a); pushVertex(d); pushVertex(b);
        pushVertex(b); pushVertex(d); pushVertex(e);
      }
    }
    const lines = [];
    const pushLine = (a, b, color = [0.22, 0.30, 0.42]) => lines.push(...a, ...color, ...b, ...color);
    const baseY = -0.83;
    for (let c = 0; c < colCount; c += Math.max(1, Math.ceil(colCount / 12))) pushLine([xAt(c), baseY, zAt(0)], [xAt(c), baseY, zAt(renderRows - 1)]);
    for (let r = 0; r < renderRows; r++) pushLine([xAt(0), baseY, zAt(r)], [xAt(colCount - 1), baseY, zAt(r)]);
    pushLine([xAt(0), baseY, zAt(0)], [xAt(colCount - 1), baseY, zAt(0)], [0.35, 0.48, 0.62]);
    pushLine([xAt(0), baseY, zAt(0)], [xAt(0), baseY, zAt(renderRows - 1)], [0.35, 0.48, 0.62]);
    pushLine([xAt(0), -0.85, zAt(0)], [xAt(0), 0.85, zAt(0)], [0.35, 0.48, 0.62]);

    const labels = [];
    const addStrikeMarker = (label, strike, color, yTop = 0.95) => {
      const x = xForStrike(Number(strike));
      if (x === null) return;
      pushLine([x, -0.82, zAt(0)], [x, yTop, zAt(renderRows - 1)], color);
      labels.push({text: label, position: [x, yTop + 0.08, zAt(renderRows - 1)], size: 0.06});
    };
    addStrikeMarker("SPOT", payload?.spot, [0.95, 0.95, 0.95], 0.92);
    const levels = payload?.levels || {};
    addStrikeMarker("CALL", levels.call_resistance, [0.13, 0.83, 0.93], 0.82);
    addStrikeMarker("PUT", levels.put_support, [0.49, 0.23, 0.93], 0.82);
    addStrikeMarker("GFLIP", levels.gamma_flip, [0.98, 0.82, 0.22], 0.72);

    labels.push({text: "Strike", position: [0, baseY - 0.17, zAt(0) - 0.25], size: 0.09});
    labels.push({text: String(payload?.greek || "GEX").toUpperCase(), position: [xAt(0) - 0.28, 0.88, zAt(0)], size: 0.085});
    const strikeStep = Math.max(1, Math.ceil(colCount / 8));
    for (let c = 0; c < colCount; c += strikeStep) labels.push({text: String(strikes[c]), position: [xAt(c), baseY - 0.08, zAt(0) - 0.08], size: 0.065});
    for (let r = 0; r < rowCount; r++) {
      const z = rowCount === 1 ? zAt(0.5) : zAt(r);
      labels.push({text: shortExpiry(expiries[r]), position: [xAt(colCount - 1) + 0.24, baseY, z], size: 0.066});
    }
    return {mesh, lines, labels};
  }

  _buffer(vertices, mode) {
    const gl = this.gl;
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(vertices), gl.STATIC_DRAW);
    return {buffer, count: vertices.length / 6, mode};
  }

  _drawColorBuffer(item, matrix, mode) {
    const gl = this.gl;
    gl.useProgram(this.colorProgram);
    const posLoc = gl.getAttribLocation(this.colorProgram, "aPosition");
    const colorLoc = gl.getAttribLocation(this.colorProgram, "aColor");
    gl.bindBuffer(gl.ARRAY_BUFFER, item.buffer);
    gl.enableVertexAttribArray(posLoc);
    gl.vertexAttribPointer(posLoc, 3, gl.FLOAT, false, 24, 0);
    gl.enableVertexAttribArray(colorLoc);
    gl.vertexAttribPointer(colorLoc, 3, gl.FLOAT, false, 24, 12);
    gl.uniformMatrix4fv(gl.getUniformLocation(this.colorProgram, "uMatrix"), false, new Float32Array(matrix));
    gl.drawArrays(mode, 0, item.count);
  }

  _makeLabel(text, position, size) {
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    const fontSize = 34;
    ctx.font = `800 ${fontSize}px Menlo, Consolas, monospace`;
    const width = Math.ceil(ctx.measureText(text).width + 18);
    canvas.width = nextPow2(width);
    canvas.height = 64;
    ctx.font = `800 ${fontSize}px Menlo, Consolas, monospace`;
    ctx.textBaseline = "middle";
    ctx.fillStyle = "rgba(203, 213, 225, 0.95)";
    ctx.fillText(text, 8, canvas.height / 2);
    const gl = this.gl;
    const texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, canvas);
    return {texture, position, width: (width / 64) * size, height: size, text};
  }

  _drawLabels(matrix) {
    const gl = this.gl;
    if (!this.labels.length) return;
    const eye = this._cameraPosition();
    const forward = norm(sub(this.view.target, eye));
    const right = norm(cross(forward, [0, 1, 0]));
    const up = norm(cross(right, forward));
    gl.useProgram(this.textProgram);
    gl.uniformMatrix4fv(gl.getUniformLocation(this.textProgram, "uMatrix"), false, new Float32Array(matrix));
    const posLoc = gl.getAttribLocation(this.textProgram, "aPosition");
    const texLoc = gl.getAttribLocation(this.textProgram, "aTexcoord");
    const buffer = gl.createBuffer();
    for (const label of this.labels) {
      const hw = label.width / 2;
      const hh = label.height / 2;
      const p = label.position;
      const r = scale(right, hw);
      const u = scale(up, hh);
      const a = sub(sub(p, r), u), b = add(sub(p, u), r), c = add(add(p, r), u), d = add(sub(p, r), u);
      const vertices = new Float32Array([
        ...a, 0, 1, ...b, 1, 1, ...d, 0, 0,
        ...b, 1, 1, ...c, 1, 0, ...d, 0, 0,
      ]);
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(posLoc);
      gl.vertexAttribPointer(posLoc, 3, gl.FLOAT, false, 20, 0);
      gl.enableVertexAttribArray(texLoc);
      gl.vertexAttribPointer(texLoc, 2, gl.FLOAT, false, 20, 12);
      gl.bindTexture(gl.TEXTURE_2D, label.texture);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
    }
    gl.deleteBuffer(buffer);
  }

  _disposeLabels() {
    if (!this.gl) return;
    for (const label of this.labels) this.gl.deleteTexture(label.texture);
    this.labels = [];
  }
}

function nextPow2(value) {
  let out = 1;
  while (out < value) out *= 2;
  return out;
}

function shortExpiry(value) {
  if (!value) return "--";
  const date = new Date(value + "T00:00:00Z");
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString("en-US", {month: "short", day: "numeric", timeZone: "UTC"});
}

export { formatCompact };
