#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");
const { performance } = require("node:perf_hooks");
const readline = require("node:readline");

function parseJson(text, source) {
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`${source} 不是合法 JSON：${error.message}`);
  }
}

function parseSecChBrands(value, major = "") {
  const text = String(value || "").trim();
  if (!text) {
    return [
      { brand: "Not)A;Brand", version: "8" },
      { brand: "Chromium", version: String(major || "") },
      { brand: "Google Chrome", version: String(major || "") },
    ];
  }
  const out = [];
  const re = /"([^"]+)"\s*;\s*v="([^"]+)"/g;
  let m;
  while ((m = re.exec(text))) out.push({ brand: m[1], version: m[2] });
  return out.length ? out : text.split(",").map((part) => ({ brand: part.trim(), version: String(major || "") })).filter((x) => x.brand);
}

function createEventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, listener) {
      const bucket = listeners.get(type) || [];
      bucket.push(listener);
      listeners.set(type, bucket);
    },
    removeEventListener(type, listener) {
      const bucket = listeners.get(type) || [];
      listeners.set(
        type,
        bucket.filter((item) => item !== listener)
      );
    },
    dispatchEvent(event) {
      const bucket = listeners.get(event.type) || [];
      for (const listener of [...bucket]) listener.call(this, event);
    },
  };
}

function btoaBinary(value) {
  return Buffer.from(String(value), "binary").toString("base64");
}

function atobBinary(value) {
  return Buffer.from(String(value), "base64").toString("binary");
}

function createStorage() {
  const values = new Map();
  return {
    get length() {
      return values.size;
    },
    key(index) {
      return [...values.keys()][Number(index)] ?? null;
    },
    getItem(key) {
      const name = String(key);
      return values.has(name) ? values.get(name) : null;
    },
    setItem(key, value) {
      values.set(String(key), String(value));
    },
    removeItem(key) {
      values.delete(String(key));
    },
    clear() {
      values.clear();
    },
  };
}

function createDomRect(width = 0, height = 0) {
  return {
    x: 0,
    y: 0,
    width,
    height,
    top: 0,
    left: 0,
    right: width,
    bottom: height,
    toJSON() {
      return {
        x: this.x,
        y: this.y,
        width: this.width,
        height: this.height,
        top: this.top,
        left: this.left,
        right: this.right,
        bottom: this.bottom,
      };
    },
  };
}


function createDomTokenList(initial = []) {
  const tokens = new Set(initial);
  const api = {
    add(...items) { for (const item of items) if (item) tokens.add(String(item)); },
    remove(...items) { for (const item of items) tokens.delete(String(item)); },
    contains(item) { return tokens.has(String(item)); },
    toggle(item, force) {
      const token = String(item);
      const shouldAdd = force === undefined ? !tokens.has(token) : Boolean(force);
      if (shouldAdd) tokens.add(token); else tokens.delete(token);
      return shouldAdd;
    },
    replace(oldToken, newToken) {
      if (!tokens.has(String(oldToken))) return false;
      tokens.delete(String(oldToken));
      tokens.add(String(newToken));
      return true;
    },
    item(index) { return [...tokens][Number(index)] || null; },
    get length() { return tokens.size; },
    toString() { return [...tokens].join(" "); },
    [Symbol.iterator]() { return tokens[Symbol.iterator](); },
  };
  Object.defineProperty(api, Symbol.toStringTag, { value: "DOMTokenList" });
  return api;
}

function createStyleDeclaration() {
  const values = Object.create(null);
  return {
    get cssText() {
      return Object.entries(values).map(([k, v]) => `${k}: ${v};`).join(" ");
    },
    set cssText(text) {
      for (const part of String(text || "").split(";")) {
        const idx = part.indexOf(":");
        if (idx > 0) this.setProperty(part.slice(0, idx).trim(), part.slice(idx + 1).trim());
      }
    },
    get length() { return Object.keys(values).length; },
    item(index) { return Object.keys(values)[Number(index)] || ""; },
    getPropertyValue(name) { return values[String(name)] || ""; },
    setProperty(name, value) { values[String(name)] = String(value); this[String(name)] = String(value); },
    removeProperty(name) { const key = String(name); const old = values[key] || ""; delete values[key]; delete this[key]; return old; },
  };
}

function createElementNode(tagName, ownerDocument, rect = createDomRect()) {
  const target = createEventTarget();
  const children = [];
  const attrs = new Map();
  const dataset = {};
  const element = {
    nodeType: 1,
    nodeName: String(tagName).toUpperCase(),
    tagName: String(tagName).toUpperCase(),
    ownerDocument,
    parentNode: null,
    parentElement: null,
    children,
    childNodes: children,
    firstChild: null,
    lastChild: null,
    style: createStyleDeclaration(),
    dataset,
    classList: createDomTokenList(),
    textContent: "",
    innerHTML: "",
    appendChild(node) {
      children.push(node);
      node.parentNode = element;
      node.parentElement = element;
      element.firstChild = children[0] || null;
      element.lastChild = children[children.length - 1] || null;
      return node;
    },
    removeChild(node) {
      const index = children.indexOf(node);
      if (index >= 0) children.splice(index, 1);
      if (node) { node.parentNode = null; node.parentElement = null; }
      element.firstChild = children[0] || null;
      element.lastChild = children[children.length - 1] || null;
      return node;
    },
    insertBefore(node, before) {
      const index = children.indexOf(before);
      if (index < 0) return this.appendChild(node);
      children.splice(index, 0, node);
      node.parentNode = element;
      node.parentElement = element;
      element.firstChild = children[0] || null;
      element.lastChild = children[children.length - 1] || null;
      return node;
    },
    remove() { if (element.parentNode?.removeChild) element.parentNode.removeChild(element); },
    setAttribute(name, value) {
      const key = String(name);
      const val = String(value);
      attrs.set(key, val);
      if (key === "class") element.classList = createDomTokenList(val.split(/\s+/).filter(Boolean));
      if (key === "id") element.id = val;
      if (key.startsWith("data-")) {
        const prop = key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        dataset[prop] = val;
      }
    },
    getAttribute(name) { return attrs.get(String(name)) ?? null; },
    hasAttribute(name) { return attrs.has(String(name)); },
    removeAttribute(name) { attrs.delete(String(name)); },
    getAttributeNames() { return [...attrs.keys()]; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    matches() { return false; },
    closest() { return null; },
    getBoundingClientRect() { return rect; },
    addEventListener: target.addEventListener,
    removeEventListener: target.removeEventListener,
    dispatchEvent: target.dispatchEvent,
    click() { target.dispatchEvent.call(element, { type: "click", target: element }); },
  };
  Object.defineProperty(element, "attributes", {
    get() { return [...attrs].map(([name, value]) => ({ name, value, nodeName: name, nodeValue: value, valueOf: () => value })); },
  });
  return element;
}


function createIntlObject(options) {
  const intlObject = Object.create(Intl);
  const NativeDateTimeFormat = Intl.DateTimeFormat;
  function DateTimeFormatMock(locales, formatOptions = {}) {
    const mergedOptions = { ...(formatOptions || {}) };
    if (options.timeZone) mergedOptions.timeZone = options.timeZone;
    const fmt = new NativeDateTimeFormat(locales || options.languages || options.language, mergedOptions);
    const nativeResolvedOptions = fmt.resolvedOptions.bind(fmt);
    Object.defineProperty(fmt, "resolvedOptions", {
      value: () => {
        const resolved = nativeResolvedOptions();
        if (options.timeZone) resolved.timeZone = options.timeZone;
        if (options.language) resolved.locale = options.language;
        return resolved;
      },
    });
    return fmt;
  }
  DateTimeFormatMock.prototype = NativeDateTimeFormat.prototype;
  Object.setPrototypeOf(DateTimeFormatMock, NativeDateTimeFormat);
  intlObject.DateTimeFormat = DateTimeFormatMock;
  return intlObject;
}

function createDateConstructor(options) {
  const NativeDate = Date;
  const zoneNames = new Intl.DateTimeFormat("en-US", {
    timeZone: options.timeZone || undefined,
    timeZoneName: "long",
  });
  const format = (date) => {
    const text = NativeDate.prototype.toString.call(date);
    if (Number.isNaN(date.getTime())) return text;
    for (const part of zoneNames.formatToParts(date)) {
      if (part.type === "timeZoneName") {
        return text.replace(/\s*\([^)]*\)$/, ` (${part.value})`);
      }
    }
    throw new Error("当前时区没有可用长名");
  };
  function BrowserDate(...args) {
    if (!new.target) return format(new NativeDate());
    const date = new NativeDate(...args);
    Object.setPrototypeOf(date, BrowserDate.prototype);
    return date;
  }
  BrowserDate.prototype = Object.create(NativeDate.prototype, {
    constructor: { value: BrowserDate, configurable: true, writable: true },
    toString: { value() { return format(this); }, configurable: true, writable: true },
  });
  Object.setPrototypeOf(BrowserDate, NativeDate);
  return BrowserDate;
}


function createPerformanceObserver(observerSet) {
  return class PerformanceObserverMock {
    constructor(callback) { this.callback = callback; this._observed = false; this._types = new Set(); }
    observe(options = {}) {
      this._observed = true;
      if (options.type) this._types.add(String(options.type));
      if (Array.isArray(options.entryTypes)) for (const type of options.entryTypes) this._types.add(String(type));
      observerSet.add(this);
    }
    disconnect() { this._observed = false; observerSet.delete(this); }
    takeRecords() { return []; }
    _notify(entry) {
      if (!this._observed) return;
      if (this._types.size && !this._types.has(entry.entryType)) return;
      try { this.callback({ getEntries: () => [entry], getEntriesByType: (type) => entry.entryType === String(type) ? [entry] : [] }); } catch {}
    }
    static get supportedEntryTypes() { return ["navigation", "resource", "paint", "mark", "measure"]; }
  };
}

function createNetworkInformation() {
  const target = createEventTarget();
  const info = {
    downlink: 10,
    effectiveType: "4g",
    rtt: 50,
    saveData: false,
    type: "wifi",
    onchange: null,
    addEventListener: target.addEventListener,
    removeEventListener: target.removeEventListener,
    dispatchEvent: target.dispatchEvent,
  };
  Object.defineProperty(info, Symbol.toStringTag, { value: "NetworkInformation" });
  return info;
}

function createCookieJar(initialCookie = "") {
  const values = new Map();
  for (const part of String(initialCookie || "").split(";")) {
    const trimmed = part.trim();
    if (!trimmed) continue;
    const idx = trimmed.indexOf("=");
    if (idx <= 0) continue;
    values.set(trimmed.slice(0, idx), trimmed.slice(idx + 1));
  }
  return {
    get cookie() {
      return [...values.entries()].map(([k, v]) => `${k}=${v}`).join("; ");
    },
    set cookie(value) {
      const first = String(value || "").split(";")[0];
      const idx = first.indexOf("=");
      if (idx > 0) values.set(first.slice(0, idx).trim(), first.slice(idx + 1).trim());
    },
  };
}

function makeNativeFunction(name, impl = () => undefined) {
  const fn = function (...args) { return impl.apply(this, args); };
  Object.defineProperty(fn, "name", { value: name });
  Object.defineProperty(fn, "toString", { value: () => `function ${name}() { [native code] }` });
  return fn;
}

function defineReadonly(target, name, value, { enumerable = true } = {}) {
  Object.defineProperty(target, name, {
    configurable: true,
    enumerable,
    get: makeNativeFunction(`get ${name}`, () => value),
  });
}

function createArrayLike(items, tagName, namedKey) {
  const proto = {};
  Object.defineProperty(proto, Symbol.toStringTag, { value: tagName, configurable: true });
  Object.defineProperties(proto, {
    item: {
      configurable: true,
      enumerable: true,
      writable: true,
      value: makeNativeFunction("item", function (index) {
        const n = Number(index);
        return Number.isInteger(n) && n >= 0 ? this[n] || null : null;
      }),
    },
    namedItem: {
      configurable: true,
      enumerable: true,
      writable: true,
      value: makeNativeFunction("namedItem", function (name) {
        const wanted = String(name);
        for (let i = 0; i < this.length; i++) {
          const item = this[i];
          if (String(item?.[namedKey] || "") === wanted) return item;
        }
        return null;
      }),
    },
    [Symbol.iterator]: {
      configurable: true,
      writable: true,
      value: Array.prototype[Symbol.iterator],
    },
  });
  const out = Object.create(proto);
  items.forEach((item, index) => Object.defineProperty(out, index, {
    value: item,
    configurable: true,
    enumerable: true,
  }));
  Object.defineProperty(out, "length", { value: items.length, configurable: true });
  return out;
}

function createPluginArray(isSafari = false) {
  const makePlugin = (name) => ({
    name,
    filename: "internal-pdf-viewer",
    description: "Portable Document Format",
    length: 2,
    item(index) { return this[index] || null; },
    namedItem(type) { return this[type] || null; },
  });
  const pdf = { type: "application/pdf", suffixes: "pdf", description: "Portable Document Format", enabledPlugin: null };
  const textPdf = { type: "text/pdf", suffixes: "pdf", description: "Portable Document Format", enabledPlugin: null };
  const pluginItems = isSafari ? [
    makePlugin("WebKit built-in PDF"),
    makePlugin("PDF Viewer"),
  ] : [
    makePlugin("PDF Viewer"),
    makePlugin("Chrome PDF Viewer"),
    makePlugin("Chromium PDF Viewer"),
    makePlugin("Microsoft Edge PDF Viewer"),
    makePlugin("WebKit built-in PDF"),
  ];
  for (const plugin of pluginItems) {
    plugin[0] = pdf;
    plugin[1] = textPdf;
    plugin["application/pdf"] = pdf;
    plugin["text/pdf"] = textPdf;
    Object.defineProperty(plugin, Symbol.toStringTag, { value: "Plugin" });
  }
  pdf.enabledPlugin = pluginItems[0];
  textPdf.enabledPlugin = pluginItems[0];
  Object.defineProperty(pdf, Symbol.toStringTag, { value: "MimeType" });
  Object.defineProperty(textPdf, Symbol.toStringTag, { value: "MimeType" });
  const plugins = createArrayLike(pluginItems, "PluginArray", "name");
  Object.defineProperty(Object.getPrototypeOf(plugins), "refresh", {
    configurable: true,
    enumerable: true,
    writable: true,
    value: makeNativeFunction("refresh"),
  });
  return plugins;
}

function createMimeTypeArray() {
  const plugin = { name: "PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format" };
  const items = [
    { type: "application/pdf", suffixes: "pdf", description: "Portable Document Format", enabledPlugin: plugin },
    { type: "text/pdf", suffixes: "pdf", description: "Portable Document Format", enabledPlugin: plugin },
  ];
  for (const item of items) Object.defineProperty(item, Symbol.toStringTag, { value: "MimeType" });
  return createArrayLike(items, "MimeTypeArray", "type");
}

function createScreen(raw = {}) {
  const values = {
    width: Number(raw.width || 0),
    height: Number(raw.height || 0),
    availWidth: Number(raw.availWidth ?? raw.width ?? 0),
    availHeight: Number(raw.availHeight ?? raw.height ?? 0),
    colorDepth: Number(raw.colorDepth || 24),
    pixelDepth: Number(raw.pixelDepth || raw.colorDepth || 24),
    availLeft: Number(raw.availLeft || 0),
    availTop: Number(raw.availTop || 0),
    isExtended: false,
    orientation: raw.orientation || { type: "landscape-primary", angle: 0 },
  };
  const proto = {};
  Object.defineProperty(proto, Symbol.toStringTag, { value: "Screen", configurable: true });
  for (const [name, value] of Object.entries(values)) defineReadonly(proto, name, value);
  return Object.create(proto);
}

function createCanvas(width = 300, height = 150, isSafari = false, gpu = {}) {
  const canvas = {
    tagName: "CANVAS",
    style: {},
    width,
    height,
    parentNode: null,
    getBoundingClientRect() { return createDomRect(this.width, this.height); },
    toDataURL() { return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lxv8dQAAAABJRU5ErkJggg=="; },
    getContext(type) {
      const name = String(type || "").toLowerCase();
      if (name === "2d") {
        return {
          canvas,
          fillStyle: "#000000",
          strokeStyle: "#000000",
          font: "10px sans-serif",
          fillRect() {}, clearRect() {}, strokeRect() {}, beginPath() {}, closePath() {}, moveTo() {}, lineTo() {}, stroke() {}, fill() {},
          fillText() {}, strokeText() {}, measureText(text) { return { width: String(text || "").length * 6.5 }; },
          getImageData() { return { data: new Uint8ClampedArray(canvas.width * canvas.height * 4), width: canvas.width, height: canvas.height }; },
          putImageData() {}, createImageData(w, h) { return { data: new Uint8ClampedArray(w * h * 4), width: w, height: h }; },
        };
      }
      if (name === "webgl" || name === "experimental-webgl" || name === "webgl2") {
        return {
          canvas,
          getParameter(param) {
            const values = new Map([
              [0x1f00, "WebKit"],                    // VENDOR
              [0x1f01, "WebKit WebGL"],              // RENDERER
              [0x1f02, isSafari ? "WebGL 2.0" : "WebGL 2.0 (OpenGL ES 3.0 Chromium)"],
              [0x8b8c, isSafari ? "WebGL GLSL ES 1.0" : "WebGL GLSL ES 3.00 (OpenGL ES GLSL ES 3.0 Chromium)"],
              [0x9245, gpu.vendor || (isSafari ? "Apple Inc." : "Google Inc. (Apple)")],
              [0x9246, gpu.renderer || (isSafari
                ? "Apple GPU"
                : "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)")],
              [0x0d33, 16384],                       // MAX_TEXTURE_SIZE
              [0x8869, 16],                          // MAX_VERTEX_ATTRIBS
              [0x0d3a, new Int32Array([16384, 16384])], // MAX_VIEWPORT_DIMS
              [0x8dfb, 1024],                        // MAX_VERTEX_UNIFORM_VECTORS
              [0x8872, 16],                          // MAX_TEXTURE_IMAGE_UNITS
            ]);
            return values.has(param) ? values.get(param) : 0;
          },
          getExtension(name) {
            if (name === "WEBGL_debug_renderer_info") {
              return { UNMASKED_VENDOR_WEBGL: 0x9245, UNMASKED_RENDERER_WEBGL: 0x9246 };
            }
            return {};
          },
          getSupportedExtensions() { return ["ANGLE_instanced_arrays", "EXT_blend_minmax", "WEBGL_debug_renderer_info", "WEBGL_lose_context"]; },
          getContextAttributes() { return { alpha: true, antialias: true, depth: true, failIfMajorPerformanceCaveat: false, powerPreference: "default", premultipliedAlpha: true, preserveDrawingBuffer: false, stencil: false, desynchronized: false, xrCompatible: false }; },
          isContextLost() { return false; },
          clearColor() {}, clear() {}, viewport() {}, createBuffer() { return {}; }, bindBuffer() {}, bufferData() {},
        };
      }
      return null;
    },
    addEventListener() {}, removeEventListener() {},
  };
  return canvas;
}

function createAudioContext() {
  return class AudioContextMock {
    constructor() { this.sampleRate = 48000; this.state = "running"; this.destination = {}; }
    createOscillator() { return { type: "sine", frequency: { value: 440 }, connect() {}, start() {}, stop() {} }; }
    createAnalyser() { return { fftSize: 2048, frequencyBinCount: 1024, getFloatFrequencyData() {}, getByteFrequencyData() {} }; }
    createGain() { return { gain: { value: 1 }, connect() {} }; }
    close() { this.state = "closed"; return Promise.resolve(); }
    resume() { this.state = "running"; return Promise.resolve(); }
    suspend() { this.state = "suspended"; return Promise.resolve(); }
  };
}

function createBrowserContext(options) {
  const windowTarget = createEventTarget();
  // SDK 内部偶尔会调用 console.log/error（例如 cookie 库弃用告警）。这些必须
  // 走 stderr，绝不能污染 stdout 协议通道。
  const vmConsole = {
    log: (...a) => process.stderr.write(a.map(String).join(" ") + "\n"),
    info: (...a) => process.stderr.write(a.map(String).join(" ") + "\n"),
    warn: (...a) => process.stderr.write(a.map(String).join(" ") + "\n"),
    error: (...a) => process.stderr.write(a.map(String).join(" ") + "\n"),
    debug: () => {},
    trace: () => {},
  };
  const managedTimers = new Set();
  const managedSetTimeout = (callback, delay, ...args) => {
    const id = setTimeout(() => {
      managedTimers.delete(id);
      callback(...args);
    }, delay);
    managedTimers.add(id);
    return id;
  };
  const managedClearTimeout = (id) => {
    managedTimers.delete(id);
    clearTimeout(id);
  };
  // 真实 SDK 内部 sid 由 crypto.randomUUID 自行生成，应用层不得注入
  // device_id / sentinel_sid / 固定种子。这里直接用 Node webcrypto 的真实随机源。
  const browserCrypto = Object.create(crypto.webcrypto);
  browserCrypto.randomUUID = () => crypto.randomUUID();
  browserCrypto.getRandomValues = crypto.webcrypto.getRandomValues.bind(crypto.webcrypto);

  const performanceObservers = new Set();
  const perfEntries = [{
    name: options.pageUrl,
    entryType: "navigation",
    startTime: 0,
    duration: Math.max(1, performance.now()),
    initiatorType: "navigation",
    nextHopProtocol: options.nextHopProtocol || "h2",
    transferSize: 0,
    encodedBodySize: 0,
    decodedBodySize: 0,
    toJSON() { return { ...this }; },
  }];
  function pushPerformanceEntry(entry) {
    perfEntries.push(entry);
    for (const observer of [...performanceObservers]) observer._notify?.(entry);
  }

  const browserIntl = createIntlObject(options);
  const browserDate = createDateConstructor(options);
  const browserScreen = createScreen(options.screen);

  const browserPerformance = {
    now: () => performance.now(),
    timeOrigin: performance.timeOrigin || Date.now() - performance.now(),
    memory: {
      jsHeapSizeLimit: options.jsHeapSizeLimit,
      totalJSHeapSize: Math.floor(options.jsHeapSizeLimit / 3),
      usedJSHeapSize: Math.floor(options.jsHeapSizeLimit / 8),
    },
    getEntries() { return perfEntries.slice(); },
    getEntriesByType(type) { return perfEntries.filter((entry) => entry.entryType === String(type)); },
    getEntriesByName(name) { return perfEntries.filter((entry) => entry.name === String(name)); },
    mark(name) { pushPerformanceEntry({ name: String(name), entryType: "mark", startTime: this.now(), duration: 0, toJSON() { return { ...this }; } }); },
    measure(name) { pushPerformanceEntry({ name: String(name), entryType: "measure", startTime: this.now(), duration: 0, toJSON() { return { ...this }; } }); },
    clearMarks() {},
    clearMeasures() {},
  };
  const mathObject = Object.create(Math);
  if (Number.isFinite(options.fixedRandom)) {
    mathObject.random = () => options.fixedRandom;
  }
  const currentScript = { src: options.scriptSrc, length: options.scriptSrc.length };
  // 注册 flow 的 SDK 运行于 Sentinel iframe；正样本生成的 p[5] 始终来自
  // 当前 Sentinel SDK，而不是父页面的 Google/Stripe/ChatGPT 脚本。
  const scripts = [currentScript];
  // Auth 注册页没有 data-build，也没有 ChatGPT c/<build> 脚本。只有明确传入
  // buildId 的 ChatGPT 页面场景才注入该脚本，避免 SDK 从伪造路径推导出 c/1/_。
  if (options.buildId) {
    const appBuildPath = String(options.buildId).startsWith("c/")
      ? String(options.buildId)
      : `c/${options.buildId}/_/`;
    const appScriptSrc = `https://chatgpt.com/${appBuildPath}ssg.js`;
    scripts.push({ src: appScriptSrc, length: appScriptSrc.length });
    scripts.push(
      { src: "https://accounts.google.com/gsi/client", length: 38 },
      { src: "https://chatgpt.com/cdn-cgi/challenge-platform/scripts/jsd/api.js?onload=jsdOnload", length: 84 },
      { src: "https://chatgpt.com/_next/static/chunks/webpack.js", length: 48 },
      { src: "https://js.stripe.com/v3/", length: 24 },
    );
  }
  const attrs = new Map();
  if (options.buildId) attrs.set("data-build", options.buildId);
  const reactListeningKey = options.reactListeningKey || "_reactListening" + crypto.randomBytes(6).toString("hex");
  const reactContainerKey = options.reactContainerKey || "__reactContainer$" + crypto.randomBytes(6).toString("hex");
  const reactResourcesKey = options.reactResourcesKey || reactContainerKey.replace("__reactContainer$", "__reactResources$");

  const cookieJar = createCookieJar(options.cookie);
  const location = new URL(options.pageUrl);
  let iframeNode = null;
  const bodyChildren = [];
  const documentTarget = createEventTarget();
  const document = {
    currentScript,
    scripts,
    get cookie() { return cookieJar.cookie; },
    set cookie(value) { cookieJar.cookie = value; },
    URL: options.pageUrl,
    documentURI: options.pageUrl,
    referrer: options.referrer || "https://auth.openai.com/",
    title: "",
    origin: location.origin,
    location,
    characterSet: "UTF-8",
    charset: "UTF-8",
    compatMode: "CSS1Compat",
    contentType: "text/html",
    readyState: "complete",
    visibilityState: "visible",
    hidden: false,
    hasFocus() { return true; },
    [reactListeningKey]: true,
    [reactContainerKey]: true,
    [reactResourcesKey]: true,
    defaultView: null,
    head: null,
    documentElement: {
      nodeType: 1,
      nodeName: "HTML",
      tagName: "HTML",
      ownerDocument: null,
      style: createStyleDeclaration(),
      clientWidth: options.screen.width,
      clientHeight: options.screen.height,
      scrollWidth: options.screen.width,
      scrollHeight: options.screen.height,
      getAttribute(name) {
        return attrs.get(name) ?? null;
      },
      setAttribute(name, value) {
        attrs.set(name, String(value));
      },
      querySelector() { return null; },
      querySelectorAll() { return []; },
      getBoundingClientRect() {
        return createDomRect(options.screen.width, options.screen.height);
      },
    },
    body: {
      nodeType: 1,
      nodeName: "BODY",
      tagName: "BODY",
      ownerDocument: null,
      parentNode: null,
      parentElement: null,
      children: bodyChildren,
      childNodes: bodyChildren,
      style: createStyleDeclaration(),
      clientWidth: options.screen.width,
      clientHeight: options.screen.height,
      getBoundingClientRect() {
        return createDomRect(options.screen.width, options.screen.height);
      },
      appendChild(node) {
        bodyChildren.push(node);
        node.parentNode = document.body;
        if (node?.tagName === "IFRAME") iframeNode = node;
        managedSetTimeout(() => node?._emitLoad?.(), 0);
        return node;
      },
      removeChild(node) {
        const index = bodyChildren.indexOf(node);
        if (index >= 0) bodyChildren.splice(index, 1);
        if (iframeNode === node) iframeNode = null;
        if (node) node.parentNode = null;
        return node;
      },
    },
    addEventListener: documentTarget.addEventListener,
    removeEventListener: documentTarget.removeEventListener,
    dispatchEvent: documentTarget.dispatchEvent,
    querySelector(selector) {
      const q = String(selector || "").toLowerCase();
      if (q === "head") return this.head;
      if (q === "body") return this.body;
      if (q === "html" || q === "documentelement") return this.documentElement;
      return null;
    },
    querySelectorAll(selector) { const item = this.querySelector(selector); return item ? [item] : []; },
    getElementById() { return null; },
    getElementsByTagName(name) {
      const n = String(name).toLowerCase();
      if (n === "script") return scripts;
      if (n === "head") return [this.head];
      if (n === "body") return [this.body];
      if (n === "html") return [this.documentElement];
      return [];
    },
    createTextNode(text) { return { nodeType: 3, nodeName: "#text", textContent: String(text || ""), parentNode: null, ownerDocument: document }; },
    createElement(tagName) {
      const lowerTag = String(tagName).toLowerCase();
      if (lowerTag === "canvas") {
        const canvas = createCanvas(300, 150, isSafari, options.gpu);
        canvas.ownerDocument = document;
        return canvas;
      }
      if (lowerTag !== "iframe") {
        return createElementNode(tagName, document);
      }

      const target = createEventTarget();
      const iframe = createElementNode("iframe", document);
      Object.assign(iframe, {
        src: "",
        width: "",
        height: "",
        sandbox: { value: "", toString() { return this.value; } },
        getBoundingClientRect() {
          return createDomRect();
        },
        contentWindow: {
          postMessage(message, origin) {
            Promise.resolve()
              .then(async () => {
                const result = await options.handleIframeMessage(message);
                windowTarget.dispatchEvent({
                  type: "message",
                  source: iframe.contentWindow,
                  origin,
                  data: {
                    type: "response",
                    requestId: message.requestId,
                    result,
                  },
                });
              })
              .catch((error) => {
                windowTarget.dispatchEvent({
                  type: "message",
                  source: iframe.contentWindow,
                  origin,
                  data: {
                    type: "response",
                    requestId: message.requestId,
                    error: error?.message || String(error),
                  },
                });
              });
          },
        },
        addEventListener: target.addEventListener,
        removeEventListener: target.removeEventListener,
        dispatchEvent: target.dispatchEvent,
        _emitLoad() {
          target.dispatchEvent.call(iframe, { type: "load", target: iframe });
        },
      });
      return iframe;
    },
  };

  document.defaultView = null;
  document.documentElement.ownerDocument = document;
  document.body.ownerDocument = document;
  document.head = createElementNode("head", document);

  const browserFamily = String(options.browserFamily || "chrome").toLowerCase();
  const isSafari = browserFamily === "safari" || /Version\/[^ ]+ Safari\//.test(String(options.userAgent || ""));
  const exposeRequestIdleCallback = !isSafari && options.requestIdleCallback !== false;
  const navigatorProto = isSafari ? {
    javaEnabled: makeNativeFunction("javaEnabled", () => false),
    sendBeacon: makeNativeFunction("sendBeacon", () => true),
    getGamepads: makeNativeFunction("getGamepads", () => []),
    webkitGetUserMedia: makeNativeFunction("webkitGetUserMedia"),
  } : {
    createAuctionNonce: makeNativeFunction("createAuctionNonce", () => crypto.randomUUID()),
    clearOriginJoinedAdInterestGroups: makeNativeFunction("clearOriginJoinedAdInterestGroups"),
    updateAdInterestGroups: makeNativeFunction("updateAdInterestGroups"),
    canLoadAdAuctionFencedFrame: makeNativeFunction("canLoadAdAuctionFencedFrame", () => false),
    getBattery: makeNativeFunction("getBattery", () => Promise.resolve({ charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1 })),
    getGamepads: makeNativeFunction("getGamepads", () => []),
    javaEnabled: makeNativeFunction("javaEnabled", () => false),
    sendBeacon: makeNativeFunction("sendBeacon", () => true),
    vibrate: makeNativeFunction("vibrate", () => false),
  };
  Object.defineProperty(navigatorProto, Symbol.toStringTag, { value: "Navigator", configurable: true });
  const navigator = Object.create(navigatorProto);
  const navigatorValues = {
    userAgent: options.userAgent,
    language: options.language,
    languages: options.languages,
    cookieEnabled: true,
    onLine: true,
    pdfViewerEnabled: true,
    doNotTrack: null,
    plugins: createPluginArray(isSafari),
    mimeTypes: createMimeTypeArray(),
    hardwareConcurrency: options.hardwareConcurrency,
    ...(isSafari ? {} : { deviceMemory: options.deviceMemory }),
    maxTouchPoints: 0,
    platform: options.navigatorPlatform || "MacIntel",
    vendor: options.navigatorVendor || (isSafari ? "Apple Computer, Inc." : "Google Inc."),
    bluetooth: { toString: () => "[object Bluetooth]" },
    ...(isSafari ? {} : { gpu: { toString: () => "[object GPU]" } }),
    connection: createNetworkInformation(),
    permissions: { query: async () => ({ state: "prompt", onchange: null }) },
    geolocation: {
      getCurrentPosition(success, error) { if (typeof error === "function") error({ code: 1, message: "User denied Geolocation" }); },
      watchPosition() { return 1; },
      clearWatch() {},
    },
    mediaDevices: {
      enumerateDevices: async () => [],
      getUserMedia: async () => { throw new Error("Permission denied"); },
    },
    storage: { estimate: async () => ({ quota: 10737418240, usage: 0 }) },
    ...(isSafari ? {} : {
      login: { toString: () => "[object NavigatorLogin]" },
      userAgentData: {
        mobile: false,
        platform: options.userAgentDataPlatform || options.secChUaPlatform || "macOS",
        brands: parseSecChBrands(options.secChUa, options.chromeMajor),
        getHighEntropyValues: async (hints = []) => {
          const values = {
            architecture: options.secChUaArch || "arm",
            bitness: options.secChUaBitness || "64",
            mobile: false,
            model: options.secChUaModel || "",
            platform: options.userAgentDataPlatform || options.secChUaPlatform || "macOS",
            platformVersion: options.secChUaPlatformVersion || "15.7.0",
            uaFullVersion: options.chromeFullVersion || "",
            fullVersionList: parseSecChBrands(options.secChUaFullVersionList, options.chromeFullVersion || options.chromeMajor),
          };
          if (!Array.isArray(hints) || hints.length === 0) return values;
          const picked = {};
          for (const hint of hints) if (hint in values) picked[hint] = values[hint];
          return picked;
        },
        toJSON() { return { brands: this.brands, mobile: this.mobile, platform: this.platform }; },
      },
    }),
  };
  // WebIDL 属性位于 Navigator.prototype，实例本身通常没有这些 enumerable
  // own properties。SDK 会随机抽取原型 key 并读取函数/属性的字符串表现。
  for (const [name, value] of Object.entries(navigatorValues)) {
    defineReadonly(navigatorProto, name, value);
  }
  // 浏览器中的 webdriver 是 Navigator 原型上的访问器，不是 navigator 自身的
  // 可枚举数据属性。保留正样本的 undefined 返回值，同时修正 descriptor 形态。
  Object.defineProperty(navigatorProto, "webdriver", {
    configurable: true,
    enumerable: true,
    get: makeNativeFunction("get webdriver", () => undefined),
  });
  const localStorage = createStorage();
  const sessionStorage = createStorage();
  const history = {
    length: 1,
    state: null,
    back() {},
    forward() {},
    go() {},
    pushState(state) {
      this.state = state ?? null;
    },
    replaceState(state) {
      this.state = state ?? null;
    },
  };

  async function browserFetch(input, init = {}) {
    const url = typeof input === "string" ? input : (input?.url || String(input));
    const start = browserPerformance.now();
    const isSentinelPing = /\/backend-api\/sentinel\/ping(?:$|[?#])/.test(url);
    if (isSentinelPing) {
      const edge = String(options.cfEdgeMsec ?? 38);
      const origin = String(options.cfOriginTtfbMsec ?? 74);
      const tcp = String(options.cfTcpRttMsec ?? 22);
      const quic = String(options.cfQuicRttMsec ?? 0);
      const duration = Math.max(1, Number(edge) + Number(origin));
      const entry = {
        name: url,
        entryType: "resource",
        initiatorType: "fetch",
        startTime: start,
        requestStart: start + 1,
        responseStart: start + Math.max(1, Number(edge)),
        responseEnd: start + duration,
        duration,
        transferSize: 300,
        encodedBodySize: 0,
        decodedBodySize: 0,
        nextHopProtocol: options.nextHopProtocol || "h2",
        toJSON() { return { ...this }; },
      };
      pushPerformanceEntry(entry);
      return new Response("", {
        status: 204,
        headers: {
          "s-cf-edge-msec": edge,
          "s-cf-origin-ttfb-msec": origin,
          "s-cf-tcp-rtt-msec": tcp,
          "s-cf-quic-rtt-msec": quic,
        },
      });
    }
    const response = await fetch(input, init);
    const end = browserPerformance.now();
    pushPerformanceEntry({
      name: url,
      entryType: "resource",
      initiatorType: init?.method ? String(init.method).toLowerCase() : "fetch",
      startTime: start,
      requestStart: start + 1,
      responseStart: Math.max(start + 1, end - 1),
      responseEnd: end,
      duration: Math.max(1, end - start),
      transferSize: 0,
      encodedBodySize: 0,
      decodedBodySize: 0,
      nextHopProtocol: options.nextHopProtocol || "h2",
      toJSON() { return { ...this }; },
    });
    return response;
  }

  const window = Object.assign(windowTarget, {
    window: null,
    self: null,
    top: null,
    parent: null,
    name: "",
    closed: false,
    length: 0,
    opener: null,
    frames: null,
    focus() {},
    blur() {},
    scrollTo() {},
    scrollBy() {},
    matchMedia(query) {
      const media = String(query);
      const normalized = media.toLowerCase().replace(/\s+/g, "");
      let matches = false;
      if (normalized.includes("prefers-color-scheme:light")) matches = true;
      if (normalized.includes("prefers-color-scheme:dark")) matches = false;
      if (normalized.includes("prefers-reduced-motion:reduce")) matches = false;
      if (normalized.includes("pointer:fine") || normalized.includes("hover:hover")) matches = true;
      const resolution = normalized.match(/(?:min-)?resolution:([\d.]+)dppx/);
      if (resolution) matches = options.devicePixelRatio >= Number(resolution[1]);
      return {
        matches,
        media,
        onchange: null,
        addListener: makeNativeFunction("addListener"),
        removeListener: makeNativeFunction("removeListener"),
        addEventListener: makeNativeFunction("addEventListener"),
        removeEventListener: makeNativeFunction("removeEventListener"),
        dispatchEvent: makeNativeFunction("dispatchEvent", () => false),
        [Symbol.toStringTag]: "MediaQueryList",
      };
    },
    getComputedStyle(element) { return element?.style || createStyleDeclaration(); },
    MessageEvent: class MessageEvent { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } },
    Event: class Event { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } },
    CustomEvent: class CustomEvent { constructor(type, init = {}) { this.type = type; this.detail = init.detail; Object.assign(this, init); } },
    DOMRect: class DOMRect { constructor(x = 0, y = 0, width = 0, height = 0) { Object.assign(this, { x, y, width, height, top: y, left: x, right: x + width, bottom: y + height }); } },
    HTMLElement: function HTMLElement() {},
    HTMLIFrameElement: function HTMLIFrameElement() {},
    MutationObserver: class MutationObserver { constructor(callback) { this.callback = callback; } observe() {} disconnect() {} takeRecords() { return []; } },
    PerformanceObserver: createPerformanceObserver(performanceObservers),
    document,
    navigator,
    screen: browserScreen,
    location,
    locationbar: { visible: true },
    menubar: { visible: true },
    personalbar: { visible: true },
    scrollbars: { visible: true },
    statusbar: { visible: true },
    toolbar: { visible: true },
    scrollX: 0,
    scrollY: 0,
    localStorage,
    sessionStorage,
    history,
    innerWidth: options.innerWidth,
    innerHeight: options.innerHeight,
    outerWidth: options.outerWidth,
    outerHeight: options.outerHeight,
    screenX: 0,
    screenY: 0,
    screenLeft: 0,
    screenTop: 0,
    pageXOffset: 0,
    pageYOffset: 0,
    devicePixelRatio: options.devicePixelRatio,
    ...(isSafari ? { safari: { pushNotification: {} } } : { chrome: { runtime: {}, app: {} } }),
    performance: browserPerformance,
    crypto: browserCrypto,
    TextEncoder,
    TextDecoder,
    URL,
    URLSearchParams,
    AbortController,
    setTimeout: managedSetTimeout,
    clearTimeout: managedClearTimeout,
    setInterval: managedSetTimeout,
    clearInterval: managedClearTimeout,
    queueMicrotask: queueMicrotask.bind(globalThis),
    btoa: btoaBinary,
    atob: atobBinary,
    fetch: browserFetch,
    console: vmConsole,
    Math: mathObject,
    Date: browserDate,
    Intl: browserIntl,
    AudioContext: createAudioContext(),
    webkitAudioContext: createAudioContext(),
    JSON,
    Array,
    Object,
    Reflect,
    Number,
    String,
    Promise,
    RegExp,
    Error,
    Map,
    Set,
    WeakMap,
    Uint8Array,
    encodeURIComponent,
    decodeURIComponent,
    unescape,
    ...(exposeRequestIdleCallback ? {
      requestIdleCallback(callback) {
        return managedSetTimeout(() => callback({ timeRemaining: () => 5, didTimeout: false }), 0);
      },
      cancelIdleCallback(id) {
        managedClearTimeout(id);
      },
    } : {}),
    requestAnimationFrame(callback) {
      return managedSetTimeout(() => callback(performance.now()), 16);
    },
    cancelAnimationFrame(id) {
      managedClearTimeout(id);
    },
    webkitRequestAnimationFrame(callback) {
      return this.requestAnimationFrame(callback);
    },
    __privateStripeFrame8094: {},
    onpageswap: null,
    ondevicemotion: null,
    onpagehide: null,
    onpageshow: null,
    onvisibilitychange: null,
    onfocus: null,
    onblur: null,
  });

  window.window = window;
  window.self = window;
  window.top = window;
  window.parent = window;
  window.frames = window;
  document.defaultView = window;

  return {
    iframeNode: () => iframeNode,
    context: vm.createContext({
      window,
      self: window,
      globalThis: window,
      document,
      navigator,
      screen: browserScreen,
      location,
      localStorage,
      sessionStorage,
      history,
      performance: browserPerformance,
      crypto: browserCrypto,
      TextEncoder,
      TextDecoder,
      URL,
      URLSearchParams,
      AbortController,
      MessageEvent: window.MessageEvent,
      Event: window.Event,
      CustomEvent: window.CustomEvent,
      DOMRect: window.DOMRect,
      HTMLElement: window.HTMLElement,
      HTMLIFrameElement: window.HTMLIFrameElement,
      MutationObserver: window.MutationObserver,
      PerformanceObserver: window.PerformanceObserver,
      setTimeout: managedSetTimeout,
      clearTimeout: managedClearTimeout,
      setInterval: managedSetTimeout,
      clearInterval: managedClearTimeout,
      queueMicrotask: queueMicrotask.bind(globalThis),
      btoa: btoaBinary,
      atob: atobBinary,
      fetch: browserFetch,
      console: vmConsole,
      Math: mathObject,
      Date: browserDate,
      Intl: browserIntl,
      AudioContext: window.AudioContext,
      webkitAudioContext: window.webkitAudioContext,
      JSON,
      Array,
      Object,
      Reflect,
      Number,
      String,
      Promise,
      RegExp,
      Error,
      Map,
      Set,
      WeakMap,
      Uint8Array,
      encodeURIComponent,
      decodeURIComponent,
      unescape,
      ...(exposeRequestIdleCallback ? {
        requestIdleCallback: window.requestIdleCallback,
        cancelIdleCallback: window.cancelIdleCallback,
      } : {}),
      requestAnimationFrame: window.requestAnimationFrame,
      cancelAnimationFrame: window.cancelAnimationFrame,
      webkitRequestAnimationFrame: window.webkitRequestAnimationFrame,
      __privateStripeFrame8094: window.__privateStripeFrame8094,
      onpageswap: window.onpageswap,
    }),
    clearTimers() {
      for (const id of [...managedTimers]) managedClearTimeout(id);
    },
  };
}

function createDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function emit(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}

function emitError(phase, category) {
  emit({ type: "error", phase, category,
    message: `Sentinel SDK ${phase} 阶段失败（${category}）` });
}

// 服务端设备值（oai-did）来自成功 warmup 的 cookie；这里确保 SDK 的
// En.getCookies()["oai-did"] 能读到与 Python 侧一致的设备值。SDK 内部 sid
// 是另一回事，由 crypto.randomUUID 自行生成，绝不在此注入。
function buildCookieHeader(deviceId, cookie) {
  const seen = new Set();
  const pairs = [];
  for (const part of String(cookie || "").split(";")) {
    const trimmed = part.trim();
    if (!trimmed) continue;
    const idx = trimmed.indexOf("=");
    if (idx <= 0) continue;
    const name = trimmed.slice(0, idx).trim();
    if (seen.has(name)) continue;
    seen.add(name);
    pairs.push(trimmed);
  }
  if (!seen.has("oai-did") && deviceId) {
    pairs.push(`oai-did=${deviceId}`);
  }
  return pairs.join("; ");
}

function buildOptions(msg, flow) {
  const screen = {
    width: Number(msg.screen?.width || 0),
    height: Number(msg.screen?.height || 0),
    availWidth: Number(msg.screen?.availWidth ?? msg.screen?.width ?? 0),
    availHeight: Number(msg.screen?.availHeight ?? msg.screen?.height ?? 0),
    colorDepth: Number(msg.screen?.colorDepth || 24),
    pixelDepth: Number(msg.screen?.pixelDepth || msg.screen?.colorDepth || 24),
    orientation: { type: "landscape-primary", angle: 0 },
  };
  const options = {
    flow,
    pageUrl: String(msg.page_url || "https://auth.openai.com/create-account/password"),
    scriptSrc: String(msg.script_src || ""),
    buildId: String(msg.build_id || ""),
    reactListeningKey: String(msg.react_listening_key || ""),
    reactContainerKey: String(msg.react_container_key || ""),
    reactResourcesKey: String(msg.react_resources_key || ""),
    cookie: buildCookieHeader(msg.device_id, msg.cookie),
    browserFamily: String(msg.browser_family || "chrome"),
    navigatorPlatform: String(msg.navigator_platform || "MacIntel"),
    navigatorVendor: String(msg.navigator_vendor || "Google Inc."),
    userAgentDataPlatform: String(msg.user_agent_data_platform || "macOS"),
    requestIdleCallback: msg.request_idle_callback === 1 || msg.request_idle_callback === true,
    language: String(msg.language || "en-US"),
    languages: Array.isArray(msg.languages)
      ? msg.languages.map(String)
      : [String(msg.language || "en-US")],
    timeZone: String(msg.time_zone || ""),
    hardwareConcurrency: Number(msg.hardware_concurrency || 4),
    jsHeapSizeLimit: Number(msg.js_heap_size_limit || 4395630592),
    deviceMemory: Number(msg.device_memory || 8),
    devicePixelRatio: Number(msg.device_pixel_ratio || 2),
    chromeMajor: String(msg.chrome_major || ""),
    chromeFullVersion: String(msg.chrome_full_version || ""),
    secChUa: String(msg.sec_ch_ua || ""),
    secChUaPlatform: String(msg.sec_ch_ua_platform || "").replace(/^"|"$/g, ""),
    secChUaFullVersionList: String(msg.sec_ch_ua_full_version_list || ""),
    secChUaPlatformVersion: String(msg.sec_ch_ua_platform_version || "").replace(/^"|"$/g, ""),
    secChUaArch: String(msg.sec_ch_ua_arch || "").replace(/^"|"$/g, ""),
    secChUaBitness: String(msg.sec_ch_ua_bitness || "").replace(/^"|"$/g, ""),
    secChUaModel: String(msg.sec_ch_ua_model || "").replace(/^"|"$/g, ""),
    userAgent: String(msg.user_agent || ""),
    screen,
    outerWidth: Number(msg.outer_width ?? screen.availWidth),
    outerHeight: Number(msg.outer_height ?? screen.availHeight),
    innerWidth: Number(msg.inner_width ?? screen.availWidth),
    innerHeight: Number(msg.inner_height ?? Math.max(0, screen.availHeight - 87)),
    gpu: {
      vendor: String(msg.webgl_vendor || ""),
      renderer: String(msg.webgl_renderer || ""),
    },
  };
  return options;
}

async function runProtocol() {
  let state = null;

  function shutdown() {
    if (!state) return;
    try {
      state.clearTimers?.();
    } catch {
      /* ignore */
    }
    state = null;
  }

  function handleRequirements(msg) {
    const flow = String(msg.flow || "");
    if (!flow) {
      emitError("requirements", "invalid_flow");
      return;
    }
    const sdkPath = path.resolve(String(msg.sdk_path || path.join(__dirname, "sdk.js")));
    if (!fs.existsSync(sdkPath)) {
      emitError("requirements", "sdk_missing");
      return;
    }

    const options = buildOptions(msg, flow);
    const solveDeferred = createDeferred();
    let requirementsEmitted = false;

    options.handleIframeMessage = async (message) => {
      if (message.type !== "token" && message.type !== "req") {
        throw new Error(`未知 iframe 消息类型：${message.type}`);
      }
      const proof = message.p;
      if (!proof || typeof proof !== "string" || !proof.trim()) {
        throw new Error("SDK 未生成有效 requirements proof");
      }
      requirementsEmitted = true;
      emit({ type: "requirements_result", flow, p: proof });
      const resolved = await solveDeferred.promise;
      return { cachedProof: proof, cachedChatReq: resolved.cachedChatReq };
    };

    let context;
    let clearTimers;
    try {
      ({ context, clearTimers } = createBrowserContext(options));
      const sdkCode = fs.readFileSync(sdkPath, "utf8");
      vm.runInContext(sdkCode, context, { filename: sdkPath });
      if (!context.SentinelSDK || typeof context.SentinelSDK.token !== "function") {
        throw new Error("SDK 加载后没有暴露 SentinelSDK.token");
      }
    } catch (error) {
      emitError("requirements", "sdk_incompatible");
      return;
    }

    const tokenPromise = Promise.resolve()
      .then(() => context.SentinelSDK.token(flow))
      .then((tokenText) => {
        if (!requirementsEmitted) {
          throw new Error("SDK token() 未请求 challenge 即返回，接口不兼容");
        }
        return { tokenText };
      })
      .catch((error) => ({ error }));

    // 若真实 SDK 在 requirements 阶段即失败（尚未发出 requirements_result），
    // 立即上报，避免 Python 等待超时。
    tokenPromise.then((result) => {
      if (result?.error && !requirementsEmitted) {
        emitError("requirements", "sdk_exec_failed");
      }
    });

    state = { context, clearTimers, flow, tokenPromise, solveDeferred };
  }

  async function handleSolve(msg) {
    if (!state) {
      emitError("solve", "no_requirements");
      return;
    }
    if (String(msg.flow || "") !== state.flow) {
      state.solveDeferred.reject(new Error("solve flow 与 requirements 不一致"));
      emitError("solve", "flow_mismatch");
      return;
    }
    const challenge = msg.challenge;
    if (!challenge || typeof challenge !== "object" || Array.isArray(challenge)) {
      state.solveDeferred.reject(new Error("solve 缺少有效 challenge"));
      emitError("solve", "invalid_challenge");
      return;
    }

    state.solveDeferred.resolve({ cachedChatReq: challenge });

    let result;
    try {
      result = await state.tokenPromise;
    } catch (error) {
      emitError("solve", "sdk_exec_failed");
      shutdown();
      return;
    }

    if (result?.error) {
      emitError("solve", "sdk_exec_failed");
      shutdown();
      return;
    }

    let tokenPayload;
    try {
      tokenPayload = parseJson(result.tokenText, "SentinelSDK.token 输出");
    } catch (error) {
      emitError("solve", "solve_invalid");
      shutdown();
      return;
    }

    let soPayload = null;
    if (typeof state.context.SentinelSDK.sessionObserverToken === "function") {
      try {
        const soText = await state.context.SentinelSDK.sessionObserverToken(state.flow);
        if (soText) {
          soPayload = typeof soText === "string"
            ? parseJson(soText, "sessionObserverToken 输出")
            : soText;
        }
      } catch {
        // SO 缺失不致命；Python 端按当前 challenge 是否要求 SO 判断。
      }
    }

    emit({
      type: "solve_result",
      flow: state.flow,
      token: tokenPayload,
      so: soPayload,
    });
    shutdown();
  }

  const rl = readline.createInterface({
    input: process.stdin,
    crlfDelay: Infinity,
    terminal: false,
  });
  rl.on("line", (line) => {
    const trimmed = String(line || "").trim();
    if (!trimmed) return;
    let msg;
    try {
      msg = JSON.parse(trimmed);
    } catch {
      emitError("protocol", "invalid_json");
      return;
    }
    if (!msg || typeof msg !== "object" || Array.isArray(msg)) {
      emitError("protocol", "invalid_json");
      return;
    }
    switch (msg.type) {
      case "requirements":
        handleRequirements(msg);
        break;
      case "solve":
        handleSolve(msg).catch(() => {});
        break;
      case "close":
        shutdown();
        emit({ type: "closed" });
        rl.close();
        break;
      default:
        emitError("protocol", "unknown_message");
    }
  });
  rl.on("close", () => {
    shutdown();
    process.exit(0);
  });
}

if (require.main === module) {
  runProtocol().catch((error) => {
    emitError("protocol", "sdk_exec_failed");
    process.exit(1);
  });
}

module.exports = {
  runProtocol,
  buildOptions,
  createBrowserContext,
};
