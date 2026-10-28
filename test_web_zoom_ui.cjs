/* Run: node --test test_web_zoom_ui.cjs
 * Execute the actual embedded application script, including registered event
 * callbacks. DOM/Canvas are minimal adapters; all HTTP and clocks are controlled.
 * These tests establish coordinate/state/request behavior, not browser layout or
 * native pointer capture. Browser QA covers those separately.
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, 'crafts_tod_native_ui.html'), 'utf8');
const script = html.match(/<script>\s*([\s\S]*?)<\/script>/)?.[1];
assert.ok(script, 'native UI must contain its executable script');
const plain = value => JSON.parse(JSON.stringify(value));
const near = (actual, expected, tolerance = 1e-8) => assert.ok(
  Math.abs(actual - expected) <= tolerance, `${actual} != ${expected}`,
);

function classList() {
  const values = new Set();
  return {
    add: (...items) => items.forEach(item => values.add(item)),
    remove: (...items) => items.forEach(item => values.delete(item)),
    contains: item => values.has(item),
    toggle(item, force) {
      const enabled = force === undefined ? !values.has(item) : Boolean(force);
      if (enabled) values.add(item); else values.delete(item);
      return enabled;
    },
  };
}

function canvasContext() {
  const calls = [];
  const ctx = {calls};
  for (const method of ['save', 'restore', 'beginPath', 'closePath', 'rect', 'clip',
    'fillRect', 'strokeRect', 'fillText', 'moveTo', 'lineTo', 'translate', 'rotate',
    'setLineDash', 'drawImage', 'fill', 'stroke']) {
    ctx[method] = (...args) => calls.push({method, args, style: ctx.strokeStyle});
  }
  ctx.createLinearGradient = () => ({addColorStop() {}});
  return ctx;
}

function makeHarness(storage = new Map()) {
  let document;
  class Element {
    constructor(tag = 'div', id = '') {
      this.tagName = tag.toUpperCase(); this.id = id; this.dataset = {};
      this.classList = classList(); this.attributes = new Map();
      this.style = {setProperty(key, value) {this[key] = value;}};
      this.events = new Map(); this.children = []; this.value = '';
      this.hidden = false; this.disabled = false; this.checked = false;
      this.textContent = ''; this.scrollHeight = 200; this.scrollTop = 0;
      this.clientHeight = id === 'spectrum' ? 200 : 350;
      this.width = 1136; this.height = this.clientHeight;
      this.context = canvasContext();
    }
    addEventListener(name, callback) {
      if (!this.events.has(name)) this.events.set(name, []);
      this.events.get(name).push(callback);
    }
    appendChild(child) {this.children.push(child); return child;}
    replaceChildren(...children) {this.children = children;}
    setAttribute(key, value) {this.attributes.set(key, String(value));}
    getAttribute(key) {return this.attributes.get(key) ?? null;}
    contains(element) {return element === this || this.children.includes(element);}
    remove() {}
    focus() {document.activeElement = this;}
    blur() {document.activeElement = document.body;}
    click() {if (!this.disabled) return this.onclick?.({target: this});}
    matches(selector) {return selector.split(',').includes(this.tagName.toLowerCase());}
    setPointerCapture() {}
    getContext() {return this.context;}
    getBoundingClientRect() {return {left: 0, top: 0, width: 1136, height: this.clientHeight};}
    async fire(name, overrides = {}) {
      const event = {
        target: this, button: 0, pointerId: 1, altKey: false, shiftKey: false,
        ctrlKey: false, metaKey: false, deltaMode: 0, deltaY: 0, deltaX: 0,
        prevented: false, preventDefault() {this.prevented = true;}, ...overrides,
      };
      for (const callback of this.events.get(name) || []) await callback(event);
      return event;
    }
  }

  const elements = new Map();
  for (const match of html.split('<script>')[0].matchAll(/<(\w+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const el = new Element(match[1], match[2]);
    el.hidden = /\bhidden(?:\s|>)/.test(match[0]);
    el.disabled = /\bdisabled(?:\s|>)/.test(match[0]);
    el.checked = /\bchecked(?:\s|>)/.test(match[0]);
    elements.set(el.id, el);
  }
  const modes = [...html.split('<script>')[0].matchAll(/<button\b[^>]*data-mode="([^"]+)"[^>]*>/g)]
    .map(match => {const id = match[0].match(/id="([^"]+)"/)?.[1]; const el = elements.get(id) || new Element('button'); el.dataset.mode = match[1]; return el;});
  const toolbarMarkup = html.split('<div class="toolbar">')[1].split('<div class="plot-stack">')[0];
  const toolbarIds = new Set([...toolbarMarkup.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
  const main = new Element('main'), plotStack = new Element();
  const body = new Element('body'), docRoot = new Element('html');
  const documentEvents = new Map();
  document = {
    body, documentElement: docRoot, activeElement: body,
    createElement: tag => new Element(tag),
    addEventListener(name, callback) {documentEvents.set(name, callback);},
    querySelector(selector) {
      if (selector.startsWith('#')) {
        assert.ok(elements.has(selector.slice(1)), `missing real DOM ID ${selector}`);
        return elements.get(selector.slice(1));
      }
      if (selector === '.main') return main;
      if (selector === '.plot-stack') return plotStack;
      throw Error(`Unimplemented DOM selector: ${selector}`);
    },
    querySelectorAll(selector) {
      if (selector === '[data-mode]') return modes;
      if (selector === '.loading-cover') return [elements.get('cover0'), elements.get('cover1')];
      if (selector === '.toolbar button,.toolbar select,.toolbar input') {
        return [...elements.values()].filter(el => toolbarIds.has(el.id) && ['BUTTON', 'SELECT', 'INPUT'].includes(el.tagName));
      }
      throw Error(`Unimplemented DOM selector: ${selector}`);
    },
  };
  elements.get('viewMode').value = 'mean';
  elements.get('beamSelect').value = 'M01';
  elements.get('spectrumY').value = 'strong';

  let clock = 1000, nextTimer = 1;
  const timers = new Map(), frames = [], requests = [], globalEvents = new Map();
  const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
  const h = {
    elements, modes, requests, timers, frames, document, storage,
    fetchHandler: null,
    flushFrames() {for (const callback of frames.splice(0)) callback(clock);},
    advance(milliseconds = 0) {
      const until = clock + milliseconds;
      for (let guard = 0; guard < 100; guard++) {
        const available = [...timers].filter(([, timer]) => timer.at <= until)
          .sort((a, b) => a[1].at - b[1].at || a[0] - b[0]);
        if (!available.length) break;
        const [id, timer] = available[0]; timers.delete(id); clock = timer.at;
        timer.callback();
        if (guard === 99) throw Error('unexpected timer loop');
      }
      clock = until;
    },
    async settle() {for (let i = 0; i < 12; i++) await Promise.resolve();},
    response(data) {return {ok: true, json: async () => data};},
    imageResponse() {
      return {ok: true, headers: {get: key => key.includes('High') ? '10' : '0'}, blob: async () => ({})};
    },
    deferImages() {
      const pending = [];
      h.fetchHandler = request => {
        assert.equal(request.url.pathname, '/api/view.jpg', 'viewport navigation must not request spectra/actions');
        const item = deferred(); pending.push({...item, request}); return item.promise;
      };
      return pending;
    },
  };
  const context = vm.createContext({
    document, devicePixelRatio: 1, innerWidth: 1440,
    location: {origin: 'http://fixture.invalid'}, URL, URLSearchParams,
    AbortController, console, performance: {now: () => clock},
    localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value)},
    setTimeout(callback, delay = 0) {const id = nextTimer++; timers.set(id, {callback, at: clock + delay}); return id;},
    clearTimeout: id => timers.delete(id), requestAnimationFrame: callback => {frames.push(callback); return frames.length;},
    addEventListener: (name, callback) => globalEvents.set(name, callback),
    ResizeObserver: class {observe() {}},
    createImageBitmap: async () => ({close() {this.closed = true;}}),
    fetch(input, options = {}) {
      const request = {url: new URL(String(input), 'http://fixture.invalid'), options};
      // The script's initial state poll is kept pending; no network or recurring
      // timer is run. Tests explicitly supply state and inspect later requests.
      if (!h.app && request.url.pathname === '/api/state') return new Promise(() => {});
      requests.push(request);
      if (h.fetchHandler) return h.fetchHandler(request);
      if (request.url.pathname === '/api/action') return Promise.resolve(h.response({state: plain(h.app.state)}));
      if (request.url.pathname === '/api/view.jpg') return Promise.resolve(h.imageResponse());
      throw Error(`Unexpected request: ${request.url}`);
    },
  });
  vm.runInContext(script, context, {filename: 'crafts_tod_native_ui.html#script'});
  h.run = code => vm.runInContext(code, context);
  h.fn = name => h.run(name);
  h.app = h.run('app');
  h.globalEvent = (name, overrides = {}) => {
    const event = {prevented: false, preventDefault() {this.prevented = true;}, ...overrides};
    globalEvents.get(name)(event); return event;
  };
  h.keyDown = (key, overrides = {}) => {
    const event = {key, target: document.body, prevented: false,
      preventDefault() {this.prevented = true;}, ...overrides};
    globalEvents.get('keydown')(event);
    return event;
  };
  const blank = () => ({frequency_ranges_mhz: [], time_ranges_records: [], boxes: []});
  h.app.state = {
    ready: true, busy: false, phase: 'ready', dirty: false, revision: 0, spectrum_revision: 0,
    group: {key: 'fixture_W_0001_T', scan: 'fixture', serial: '0001', beams: ['M01', 'M02']},
    index: 0, group_count: 2, groups: [{index: 0, serial: '0001'}, {index: 1, serial: '0002'}],
    frequency_range: [1000, 1100], frequency_step_mhz: .1, shape: [200, 1001, 2],
    view_mode: 'mean', selected_beam: 'M01', document: {polarizations: [blank(), blank()], beam_polarizations: {M01: [blank(), blank()]}},
    auto_available: {one_channel: 19, sat1380: 19}, logs: [], message: 'fixture',
  };
  h.app.view = {f0: 1000, f1: 1100, t0: 0, t1: 200};
  h.app.spectrumKey = JSON.stringify(['fixture_W_0001_T', 0, true, 'mean', 'M01']);
  h.primeImages = () => {
    h.app.images = [{close() {}}, {close() {}}];
    h.app.imageViews = [plain(h.app.view), plain(h.app.view)];
    h.app.imageKeys = [h.fn('currentImageKey')(), h.fn('currentImageKey')()];
    h.app.imageQuality = ['final', 'final'];
  };
  h.primeImages();
  h.at = (f, t, modifiers = {}) => ({
    clientX: 62 + (f - h.app.view.f0) / (h.app.view.f1 - h.app.view.f0) * 1000,
    clientY: 23 + (h.app.view.t1 - t) / (h.app.view.t1 - h.app.view.t0) * 300,
    ...modifiers,
  });
  return h;
}

test('Alt horizontal-only pointer drag changes frequency and never posts an edit', async () => {
  const h = makeHarness(), c = h.elements.get('pol0');
  const from = h.at(1020, 100, {altKey: true}), to = h.at(1080, 100);
  await c.fire('pointerdown', from); await c.fire('pointermove', to); await c.fire('pointerup', to);
  assert.deepEqual(plain(h.app.view), {f0: 1020, f1: 1080, t0: 0, t1: 200});
  assert.equal(h.requests.length, 0);
  assert.equal(h.app.history.length, 1);
});

test('Shift vertical-only pointer drag changes records and never posts an edit', async () => {
  const h = makeHarness(), c = h.elements.get('pol1');
  const from = h.at(1050, 40, {shiftKey: true}), to = h.at(1050, 160);
  await c.fire('pointerdown', from); await c.fire('pointermove', to); await c.fire('pointerup', to);
  assert.deepEqual(plain(h.app.view), {f0: 1000, f1: 1100, t0: 40, t1: 160});
  assert.equal(h.requests.length, 0);
});

test('combined modifiers and explicit zoom tools remain navigation even in a mask mode', async () => {
  for (const kind of ['both-modifiers', 'zoom-frequency', 'zoom-time']) {
    const h = makeHarness(), c = h.elements.get('pol0');
    h.app.mode = 'delete';
    const modifiers = kind === 'both-modifiers' ? {altKey: true, shiftKey: true} : {};
    if (kind !== 'both-modifiers') h.modes.find(button => button.dataset.mode === kind).click();
    const from = h.at(1020, 40, modifiers), to = h.at(1080, 160);
    await c.fire('pointerdown', from); await c.fire('pointerup', to);
    assert.equal(h.requests.length, 0, kind);
    assert.equal(h.app.state.dirty, false, kind);
    assert.ok(!h.fn('sameView')(h.app.view, {f0: 1000, f1: 1100, t0: 0, t1: 200}), kind);
  }
});

test('ordinary mask drag posts exactly one edit with scientific coordinates and pol scope', async () => {
  const h = makeHarness(), c = h.elements.get('pol1');
  h.app.mode = 'box'; h.elements.get('both').checked = true;
  const from = h.at(1020, 40), to = h.at(1080, 160);
  await c.fire('pointerdown', from); await c.fire('pointerup', to);
  const actions = h.requests.filter(request => request.url.pathname === '/api/action');
  assert.equal(actions.length, 1);
  assert.deepEqual(JSON.parse(actions[0].options.body), {
    action: 'edit', mode: 'box', pol: 1, both: true, f0: 1020, f1: 1080, t0: 40, t1: 160,
  });
  assert.deepEqual(plain(h.app.view), {f0: 1000, f1: 1100, t0: 0, t1: 200});
});

test('mask authoring waits for a final viewport but modifier navigation remains available', async () => {
  const h = makeHarness(), c = h.elements.get('pol0');
  h.app.imageQuality = ['preview', 'preview'];
  await c.fire('pointerdown', h.at(1020, 40));
  assert.equal(h.app.drag, null);
  await c.fire('pointerdown', h.at(1020, 40, {altKey: true}));
  assert.equal(h.app.drag.kind, 'zoom-frequency');
  assert.equal(h.requests.length, 0);
});

test('wheel scale respects delta magnitude and normalized pixel/line/page units', () => {
  const scale = makeHarness().fn('wheelScale');
  const a = scale({deltaY: 1, deltaMode: 0}, 350);
  const b = scale({deltaY: 10, deltaMode: 0}, 350);
  near(Math.log(b), 10 * Math.log(a));
  near(scale({deltaY: 1, deltaMode: 1}, 350), scale({deltaY: 16, deltaMode: 0}, 350));
  near(scale({deltaY: .1, deltaMode: 2}, 350), scale({deltaY: 35, deltaMode: 0}, 350));
  near(scale({deltaY: 0, deltaX: -8, deltaMode: 0}, 350), Math.exp(-.016));
});

test('zoomAt keeps the pointer anchor stable and stops drifting at minimum/full limits', () => {
  const h = makeHarness(), zoom = h.fn('zoomAt'), state = h.app.state;
  const view = {f0: 1020, f1: 1080, t0: 30, t1: 170}, anchor = 1040;
  const scaled = zoom(view, 'frequency', anchor, .5, state);
  near((anchor - scaled.f0) / (scaled.f1 - scaled.f0), 1 / 3);
  assert.equal(scaled.t0, 30); assert.equal(scaled.t1, 170);
  const minimum = zoom(view, 'frequency', anchor, 1e-20, state);
  near(minimum.f1 - minimum.f0, .05);
  const repeated = zoom(minimum, 'frequency', anchor, .5, state);
  near(repeated.f0, minimum.f0); near(repeated.f1, minimum.f1);
  const full = zoom(view, 'frequency', anchor, 1e20, state);
  near(full.f0, 1000); near(full.f1, 1100);
  assert.equal(full.t0, 30); assert.equal(full.t1, 170);
  const fullAgain = zoom(full, 'frequency', anchor, 2, state);
  near(fullAgain.f0, full.f0); near(fullAgain.f1, full.f1);
  const timeMin = zoom(view, 'time', 90, 1e-20, state);
  near(timeMin.t1 - timeMin.t0, 2);
});

test('actual wheel callback updates local view immediately, preserves anchor, and does not fetch a spectrum', async () => {
  const h = makeHarness(), c = h.elements.get('pol0');
  const anchor = 1030;
  const event = await c.fire('wheel', h.at(anchor, 100, {altKey: true, deltaY: -30}));
  assert.equal(event.prevented, true);
  near((anchor - h.app.view.f0) / (h.app.view.f1 - h.app.view.f0), .3);
  near(h.app.view.f1 - h.app.view.f0, 100 * Math.exp(-.06));
  assert.equal(h.requests.length, 0, 'network requests are scheduled, not on every wheel callback');
  h.advance(0); await h.settle();
  assert.equal(h.requests.length, 2);
  assert.ok(h.requests.every(request => request.url.pathname === '/api/view.jpg'));
  assert.equal(h.app.history.length, 1);
});

test('wheel without modifier or outside the plot does not navigate or prevent default', async () => {
  const h = makeHarness(), c = h.elements.get('pol0'), before = plain(h.app.view);
  const ordinary = await c.fire('wheel', h.at(1050, 100, {deltaY: -100}));
  const outside = await c.fire('wheel', {clientX: 0, clientY: 0, altKey: true, deltaY: -100});
  assert.equal(ordinary.prevented, false); assert.equal(outside.prevented, false);
  assert.deepEqual(plain(h.app.view), before); assert.equal(h.requests.length, 0);
});

test('cached bitmap affine mapping preserves source frequency/record positions while zooming', () => {
  const destination = makeHarness().fn('imageDestination');
  const source = {f0: 1000, f1: 1100, t0: 0, t1: 200};
  const view = {f0: 1025, f1: 1075, t0: 50, t1: 150};
  const rect = {x: 62, y: 23, w: 1000, h: 300};
  const d = destination(source, view, rect);
  assert.deepEqual(plain(d), {x: -438, y: -127, w: 2000, h: 600});
  near(d.x + (1040 - source.f0) / 100 * d.w, rect.x + (1040 - view.f0) / 50 * rect.w);
  near(d.y + (source.t1 - 120) / 200 * d.h, rect.y + (view.t1 - 120) / 100 * rect.h);
});

test('spectrum display decimation retains a one-channel spike, trough and NaN break', () => {
  const points = makeHarness().fn('spectrumPoints');
  const frequency = Array.from({length: 5000}, (_, index) => index), values = frequency.map(() => 1);
  values[1735] = 1e6; values[1736] = -1000;
  for (let i = 2500; i <= 2503; i++) values[i] = NaN;
  const result = points(frequency, values, {f0: 0, f1: 4999}, 100);
  assert.ok(result.some(point => point?.[0] === 1735 && point[1] === 1e6));
  assert.ok(result.some(point => point?.[0] === 1736 && point[1] === -1000));
  const gap = result.findIndex(point => point === null);
  assert.ok(gap > 0 && gap < result.length - 1);
  assert.ok(result[gap - 1][0] < 2500 && result[gap + 1][0] > 2503);
  assert.ok(result.length <= 408, `expected bounded pixel-bin representation, got ${result.length}`);
  const finite = result.filter(Boolean);
  assert.equal(finite[0][0], 0); assert.equal(finite.at(-1)[0], 4999);
  for (let i = 1; i < finite.length; i++) assert.ok(finite[i][0] > finite[i - 1][0]);
});

test('render scheduler holds only one image pair in flight and coalesces to the latest viewport', async () => {
  const h = makeHarness(), pending = h.deferImages();
  h.fn('renderAll')(); h.advance(0);
  assert.equal(pending.length, 2); assert.equal(h.app.renderRunning, true);
  h.app.view = {f0: 1010, f1: 1090, t0: 20, t1: 180};
  h.fn('navigationChanged')(); h.advance(120);
  h.app.view = {f0: 1030, f1: 1060, t0: 70, t1: 120};
  h.fn('navigationChanged')(true); h.advance(0);
  assert.equal(pending.length, 2, 'no second pair may start while first is still pending');
  pending[0].resolve(h.imageResponse()); pending[1].resolve(h.imageResponse()); await h.settle();
  h.advance(0); await h.settle();
  assert.equal(pending.length, 4);
  for (const item of pending.slice(2)) {
    assert.equal(item.request.url.searchParams.get('f0'), '1030');
    assert.equal(item.request.url.searchParams.get('f1'), '1060');
    assert.equal(item.request.url.searchParams.get('t0'), '70');
    assert.equal(item.request.url.searchParams.get('t1'), '120');
  }
  pending[2].resolve(h.imageResponse()); pending[3].resolve(h.imageResponse()); await h.settle();
  assert.equal(h.app.renderRunning, false); assert.equal(h.app.renderPending, false);
  assert.equal(h.fn('viewIsClear')(), true);
  assert.ok(h.requests.every(request => request.url.pathname === '/api/view.jpg'));
});

test('response from an invalidated data scope cannot install a stale bitmap', async () => {
  const h = makeHarness(), pending = h.deferImages();
  h.fn('renderAll')(); h.advance(0);
  h.fn('invalidateImages')();
  for (const item of pending) item.resolve(h.imageResponse());
  await h.settle();
  assert.equal(h.app.images[0], null); assert.equal(h.app.images[1], null);
  assert.equal(h.fn('viewIsClear')(), false);
});

test('Single-beam spectrum keeps server-supplied Mean reference separate from current-beam lines', () => {
  const h = makeHarness();
  h.elements.get('viewMode').value = 'single'; h.elements.get('beamSelect').value = 'M01';
  const data = {
    group_key: h.app.state.group.key, selected_ready: true, mean_ready: true,
    frequency: [1000, 1050, 1100], pol0: [10, 100, 10], pol1: [20, 200, 20],
    mean_pol0: [1, 2, 1], mean_pol1: [3, 4, 3],
    view_mode: 'single', view_label: 'M01', apply_auto: true,
  };
  const before = plain(data);
  h.fn('drawSpectrum')(data);
  const series = h.app.spectrumDrawCache.series;
  assert.equal(series.length, 4);
  assert.equal(series[0].reference, true); assert.equal(series[1].reference, true);
  assert.equal(series[2].reference, false); assert.equal(series[3].reference, false);
  assert.deepEqual(plain(series[0].v), [1, 2, 1]);
  assert.deepEqual(plain(series[2].v), [10, 100, 10]);
  assert.deepEqual(plain(data), before, 'rendering must not overwrite scientific spectrum values');
});

test('switching beam with the same spectrum object cannot reuse the prior beam draw cache', () => {
  const h = makeHarness(), canvas = h.elements.get('spectrum');
  h.elements.get('viewMode').value = 'single'; h.elements.get('beamSelect').value = 'M01';
  const data = {
    group_key: h.app.state.group.key, selected_ready: true, mean_ready: true,
    frequency: [1000, 1050, 1100], pol0: [10, 100, 10], pol1: [20, 200, 20],
    mean_pol0: [1, 2, 1], mean_pol1: [3, 4, 3],
    view_mode: 'single', view_label: 'M01', apply_auto: true,
  };
  h.fn('drawSpectrum')(data);
  h.elements.get('beamSelect').value = 'M02';
  canvas.context.calls.length = 0;
  h.fn('drawSpectrum')(data); // Late redraw before the new beam payload arrives.
  const lines = canvas.context.calls.filter(call => call.method === 'stroke'
    && ['#57c7ff', '#ffb15b'].includes(call.style));
  assert.equal(lines.length, 2, 'only the two Mean reference lines may remain, never old M01 as M02');
  assert.ok(h.app.spectrumDrawCache.series.every(line => line.reference));
});

test('Mean mode must not relabel a stale Single-beam payload as the Mean spectrum', () => {
  const h = makeHarness(), canvas = h.elements.get('spectrum');
  h.elements.get('viewMode').value = 'single';
  const data = {
    group_key: h.app.state.group.key, selected_ready: true, mean_ready: true,
    frequency: [1000, 1050, 1100], pol0: [10, 100, 10], pol1: [20, 200, 20],
    mean_pol0: [1, 2, 1], mean_pol1: [3, 4, 3],
    view_mode: 'single', view_label: 'M01', apply_auto: true,
  };
  h.fn('drawSpectrum')(data);
  h.elements.get('viewMode').value = 'mean';
  canvas.context.calls.length = 0;
  h.fn('drawSpectrum')(data);
  const lines = canvas.context.calls.filter(call => call.method === 'stroke'
    && ['#57c7ff', '#ffb15b'].includes(call.style));
  // Showing an independently available Mean is fine; showing no active line
  // until a matching response arrives is also fine. Reusing M01 is not.
  if (lines.length) {
    assert.ok(h.app.spectrumDrawCache.series.every(line => line.v !== data.pol0 && line.v !== data.pol1),
      'Mean view must not draw the obsolete Single-beam intensity arrays');
  }
});

test('a completed all-invalid spectrum reports no valid samples rather than computing', () => {
  const h = makeHarness(), canvas = h.elements.get('spectrum');
  h.fn('drawSpectrum')({
    group_key: h.app.state.group.key, selected_ready: true, mean_ready: false,
    frequency: [1000, 1050, 1100], pol0: [null, null, null], pol1: [null, null, null],
    view_mode: 'mean', view_label: '19-beam Mean', apply_auto: true,
  });
  const labels = canvas.context.calls.filter(call => call.method === 'fillText').map(call => String(call.args[0]));
  assert.ok(labels.some(label => label.includes('无有效谱线样本')));
  assert.ok(!labels.some(label => label.includes('计算中')));
});

test('image request error survives render finally/status redraw and clears after a successful retry', async () => {
  const h = makeHarness();
  h.fetchHandler = request => Promise.resolve(request.url.searchParams.get('pol') === '0'
    ? {ok: false, statusText: 'Synthetic render failure'} : h.imageResponse());
  h.fn('renderAll')(); h.advance(0); await h.settle();
  assert.equal(h.app.renderRunning, false);
  assert.match(h.elements.get('viewStatus').textContent, /图像请求失败/);
  h.fn('updateViewStatus')(); h.flushFrames();
  assert.match(h.elements.get('viewStatus').textContent, /图像请求失败/);
  h.fetchHandler = () => Promise.resolve(h.imageResponse());
  h.fn('renderAll')(); h.advance(0); await h.settle();
  assert.equal(h.app.renderRunning, false);
  assert.ok(!h.elements.get('viewStatus').textContent.includes('失败'));
  assert.equal(h.fn('viewIsClear')(), true);
});

function toggleSpectrum(h, request) {
  const enabled = request.url.searchParams.get('apply_auto') === '1';
  const view = request.url.searchParams.get('view_mode');
  return {
    group_key: h.app.state.group.key, revision: 1, apply_auto: enabled,
    view_mode: view, view_label: view === 'single' ? 'M01' : '19-beam Mean',
    frequency: [1000, 1100], pol0: enabled ? [1, 2] : [10, 20], pol1: [3, 4],
    selected_ready: true, ...(view === 'single' ? {mean_pol0: [5, 6], mean_pol1: [7, 8], mean_ready: true} : {}),
  };
}

test('automatic master switch updates all three plots, preserves manual edits and overlay preference', async () => {
  for (const view of ['mean', 'single']) for (const overlay of [true, false]) {
    const h = makeHarness(), master = h.elements.get('applyAuto'), show = h.elements.get('showAuto');
    h.app.state.view_mode = view; h.elements.get('viewMode').value = view;
    show.checked = overlay;
    h.app.state.document.polarizations[0].frequency_ranges_mhz = [[1020, 1030]];
    h.app.state.dirty = true;
    const manualBefore = plain(h.app.state.document);
    h.fetchHandler = request => {
      if (request.url.pathname === '/api/view.jpg') return Promise.resolve(h.imageResponse());
      assert.equal(request.url.pathname, '/api/spectrum', 'toggle must never edit, save or reload the group');
      return Promise.resolve(h.response(toggleSpectrum(h, request)));
    };
    for (const enabled of [false, true]) {
      h.requests.length = 0;
      master.checked = enabled; master.onchange(); h.advance(0); await h.settle();
      const images = h.requests.filter(r => r.url.pathname === '/api/view.jpg');
      const spectra = h.requests.filter(r => r.url.pathname === '/api/spectrum');
      assert.equal(images.length, 2); assert.equal(spectra.length, 1);
      for (const request of [...images, ...spectra]) {
        assert.equal(request.url.searchParams.get('apply_auto'), enabled ? '1' : '0');
      }
      for (const request of images) {
        assert.equal(request.url.searchParams.get('show_auto'), enabled && overlay ? '1' : '0');
      }
      assert.equal(show.checked, overlay, 'master must remember independent overlay preference');
      assert.equal(show.disabled, !enabled);
      assert.match(h.elements.get('autoMaskStatus').textContent, enabled ? /三图已启用/ : /三图已停用/);
      assert.deepEqual(plain(h.app.spectrumData.pol0), enabled ? [1, 2] : [10, 20]);
      assert.deepEqual(plain(h.app.state.document), manualBefore);
      assert.equal(h.app.state.dirty, true);
      h.fn('applyState')(plain(h.app.state));
      assert.equal(show.disabled, !enabled, 'state polls must keep overlay disabled when master is off');
    }
  }
});

test('rapid automatic toggles reject the old spectrum response', async () => {
  const h = makeHarness(), master = h.elements.get('applyAuto'), pending = [];
  h.fetchHandler = request => {
    if (request.url.pathname === '/api/view.jpg') return Promise.resolve(h.imageResponse());
    assert.equal(request.url.pathname, '/api/spectrum');
    return new Promise(resolve => pending.push({request, resolve}));
  };
  master.checked = false; master.onchange();
  master.checked = true; master.onchange();
  assert.equal(pending.length, 1);
  pending[0].resolve(h.response(toggleSpectrum(h, pending[0].request)));
  await h.settle();
  assert.equal(pending.length, 2);
  assert.equal(h.app.spectrumData.frequency.length, 0, 'outdated unmasked spectrum must not be drawn');
  pending[1].resolve(h.response(toggleSpectrum(h, pending[1].request)));
  await h.settle();
  assert.equal(h.app.spectrumData.apply_auto, true);
  assert.deepEqual(plain(h.app.spectrumData.pol0), [1, 2]);
});

test('A and V shortcuts use the same guarded display controls even after clicking a checkbox', async () => {
  const h = makeHarness();
  h.fetchHandler = request => {
    if (request.url.pathname === '/api/view.jpg') return Promise.resolve(h.imageResponse());
    assert.equal(request.url.pathname, '/api/spectrum');
    return Promise.resolve(h.response(toggleSpectrum(h, request)));
  };
  const checkbox = h.elements.get('applyAuto');
  assert.equal(h.keyDown('a', {target: checkbox}).prevented, true);
  h.advance(0); await h.settle();
  assert.equal(checkbox.checked, false);
  assert.equal(h.app.spectrumData.apply_auto, false);
  const count = h.requests.length;
  h.keyDown('v'); // Disabled overlay must not be toggled back on.
  assert.equal(h.elements.get('showAuto').checked, true);
  assert.equal(h.requests.length, count);
  h.keyDown('a'); h.advance(0); await h.settle();
  assert.equal(checkbox.checked, true);
  h.keyDown('v', {target: checkbox}); h.advance(0); await h.settle();
  assert.equal(h.elements.get('showAuto').checked, false);
  assert.equal(checkbox.checked, true, 'overlay shortcut must not disable mask application');
  assert.equal(h.requests.at(-1).url.searchParams.get('show_auto'), '0');
  assert.ok(h.requests.every(r => r.url.pathname !== '/api/action'));
});

test('P shortcut changes the next edit scope without rewriting existing manual regions', async () => {
  const h = makeHarness(), c = h.elements.get('pol1');
  h.app.mode = 'box';
  const before = plain(h.app.state.document);
  h.keyDown('p', {target: h.elements.get('both')});
  h.keyDown('p', {repeat: true});
  assert.equal(h.elements.get('both').checked, true);
  assert.deepEqual(plain(h.app.state.document), before);
  assert.equal(h.app.state.dirty, false);
  assert.equal(h.requests.length, 0);
  await c.fire('pointerdown', h.at(1020, 40)); await c.fire('pointerup', h.at(1080, 160));
  const edit = h.requests.find(r => r.url.pathname === '/api/action');
  assert.equal(JSON.parse(edit.options.body).both, true);
});

test('new shortcuts ignore typing, composition, modifiers, busy state and save dialog', () => {
  for (const name of ['ctrl', 'meta', 'alt', 'composition', 'repeat', 'select', 'text', 'busy', 'modal']) {
    const h = makeHarness();
    const event = {ctrlKey: name === 'ctrl', metaKey: name === 'meta', altKey: name === 'alt',
      isComposing: name === 'composition', repeat: name === 'repeat'};
    if (name === 'select') event.target = h.elements.get('viewMode');
    if (name === 'text') event.target = {isContentEditable: true};
    if (name === 'busy') h.app.state.busy = true;
    if (name === 'modal') h.elements.get('saveConfirm').hidden = false;
    for (const key of ['a', 'v', 'p']) h.keyDown(key, event);
    assert.equal(h.elements.get('applyAuto').checked, true, name);
    assert.equal(h.elements.get('showAuto').checked, true, name);
    assert.equal(h.elements.get('both').checked, false, name);
    assert.equal(h.requests.length, 0, name);
  }
});

test('custom shortcut replaces the old key and updates labels, accessibility and persisted settings', () => {
  const h = makeHarness();
  assert.equal(h.fn('setShortcutBinding')('frequency', 'k'), true);
  assert.equal(h.elements.get('key-frequency').textContent, 'K');
  assert.equal(h.elements.get('toolFrequency').getAttribute('aria-keyshortcuts'), 'K');
  h.app.mode = 'box'; h.keyDown('f'); assert.equal(h.app.mode, 'box');
  h.keyDown('k'); assert.equal(h.app.mode, 'frequency');
  const restored = makeHarness(h.storage);
  assert.equal(restored.elements.get('key-frequency').textContent, 'K');
  restored.app.mode = 'box'; restored.keyDown('k'); assert.equal(restored.app.mode, 'frequency');
  restored.elements.get('resetShortcuts').click();
  assert.equal(restored.elements.get('key-frequency').textContent, 'F');
  restored.app.mode = 'box'; restored.keyDown('k'); assert.equal(restored.app.mode, 'box');
});

test('shortcut conflicts are rejected, clearing unbinds, and capture supports Shift without dispatching an action', () => {
  const h = makeHarness();
  assert.equal(h.fn('setShortcutBinding')('save', 'f'), false);
  assert.equal(h.elements.get('key-save').textContent, 'S');
  assert.match(h.elements.get('shortcutMessage').textContent, /频率标记/);
  h.fn('setShortcutBinding')('frequency', '');
  assert.equal(h.elements.get('key-frequency').hidden, true);
  const field = h.fn('shortcutFields').frequency;
  let stopped = false;
  field.onkeydown({key: 'K', shiftKey: true, preventDefault() {}, stopPropagation() {stopped = true;}});
  assert.ok(stopped); assert.equal(h.requests.length, 0);
  assert.equal(h.elements.get('key-frequency').textContent, 'Shift+K');
  h.app.mode = 'box'; h.keyDown('k'); assert.equal(h.app.mode, 'box');
  h.keyDown('K', {shiftKey: true}); assert.equal(h.app.mode, 'frequency');
  field.onkeydown({key: 'Escape', preventDefault() {}, stopPropagation() {}});
  assert.equal(h.elements.get('key-frequency').textContent, 'Shift+K');
  assert.equal(h.fn('setShortcutBinding')('frequency', 'Ctrl+k'), false);
});

test('invalid stored shortcut data falls back to usable defaults', () => {
  for (const data of ['not JSON', '{"frequency":"s"}', '{"save":"Ctrl+s"}', 'null']) {
    const h = makeHarness(new Map([['crafts.shortcuts.v1', data]]));
    assert.equal(h.elements.get('key-frequency').textContent, 'F');
    assert.equal(h.elements.get('key-save').textContent, 'S');
    assert.match(h.elements.get('shortcutMessage').textContent, /恢复默认/);
  }
});

test('unsaved navigation opens a focused choice dialog; cancel and Escape keep every edit without requests', () => {
  for (const direction of ['previous', 'next']) {
    const h = makeHarness(); h.app.state.dirty = true;
    h.app.state.document.polarizations[0].frequency_ranges_mhz = [[1020, 1030]];
    const before = plain(h.app.state.document);
    h.fn('requestNavigation')(direction);
    assert.equal(h.elements.get('navigateConfirm').hidden, false);
    assert.equal(h.document.activeElement.id, 'saveNavigate');
    assert.match(h.elements.get('saveNavigateLabel').textContent, /保存并跳到/);
    assert.equal(h.requests.length, 0);
    h.keyDown('Tab'); assert.equal(h.document.activeElement.id, 'cancelNavigate');
    h.keyDown('Escape');
    assert.equal(h.elements.get('navigateConfirm').hidden, true);
    assert.deepEqual(plain(h.app.state.document), before); assert.equal(h.app.state.dirty, true);
    assert.equal(h.requests.length, 0);
  }
});

test('save-and-navigate waits for the server and blocks double submission', async () => {
  const h = makeHarness(); h.app.state.dirty = true;
  let finish;
  h.fetchHandler = request => {
    assert.equal(request.url.pathname, '/api/action');
    return new Promise(resolve => {finish = resolve;});
  };
  h.fn('requestNavigation')('next');
  const operation = h.elements.get('saveNavigate').onclick();
  assert.equal(h.elements.get('cancelNavigate').disabled, true);
  assert.equal(h.app.state.group.serial, '0001');
  h.elements.get('saveNavigate').onclick(); h.keyDown('Escape');
  assert.equal(h.requests.length, 1); assert.equal(h.elements.get('navigateConfirm').hidden, false);
  assert.deepEqual(JSON.parse(h.requests[0].options.body), {
    action: 'navigate', direction: 'next', group_key: 'fixture_W_0001_T', revision: 0, resolution: 'save',
  });
  finish(h.response({state: {...plain(h.app.state), dirty: false}})); await operation;
  assert.equal(h.elements.get('navigateConfirm').hidden, true);
  assert.equal(h.app.navigationPending, false);
});

test('failed save-and-navigate retains edits, keeps dialog open and allows retry or cancel', async () => {
  const h = makeHarness(); h.app.state.dirty = true;
  h.app.state.document.polarizations[0].frequency_ranges_mhz = [[1020, 1030]];
  const before = plain(h.app.state.document);
  h.fetchHandler = () => Promise.resolve({ok: false, json: async () => ({error: 'disk full', state: plain(h.app.state)})});
  h.fn('requestNavigation')('next'); await h.elements.get('saveNavigate').onclick();
  assert.equal(h.requests.length, 1); assert.equal(h.app.state.group.serial, '0001');
  assert.equal(h.elements.get('navigateConfirm').hidden, false);
  assert.match(h.elements.get('navigateError').textContent, /disk full/);
  assert.equal(h.elements.get('saveNavigate').disabled, false);
  assert.deepEqual(plain(h.app.state.document), before); assert.equal(h.app.state.dirty, true);
  h.elements.get('cancelNavigate').click(); assert.equal(h.elements.get('navigateConfirm').hidden, true);
});

test('discard navigation is one guarded request and never invokes save or revert separately', async () => {
  const h = makeHarness(); h.app.state.dirty = true;
  h.fn('requestNavigation')('previous'); await h.elements.get('discardNavigate').onclick();
  assert.equal(h.requests.length, 1);
  const payload = JSON.parse(h.requests[0].options.body);
  assert.equal(payload.action, 'navigate'); assert.equal(payload.resolution, 'discard');
  assert.equal(payload.direction, 'previous'); assert.equal(h.elements.get('navigateConfirm').hidden, true);
});

test('a changed server revision requires a fresh navigation choice rather than reusing stale consent', async () => {
  const h = makeHarness(); h.app.state.dirty = true;
  h.fetchHandler = () => Promise.resolve({ok: false, json: async () => ({
    error: 'current revision changed', state: {...plain(h.app.state), revision: 2},
  })});
  h.fn('requestNavigation')('next'); await h.elements.get('saveNavigate').onclick();
  assert.equal(h.elements.get('saveNavigate').disabled, true);
  assert.equal(h.elements.get('discardNavigate').disabled, true);
  assert.equal(h.elements.get('cancelNavigate').disabled, false);
  h.elements.get('cancelNavigate').click();
  assert.equal(h.elements.get('navigateConfirm').hidden, true);
  assert.equal(h.app.state.dirty, true);
});

test('clean navigation needs no dialog; dirty page unload is guarded', async () => {
  const h = makeHarness();
  assert.equal(h.globalEvent('beforeunload').prevented, false);
  await h.fn('requestNavigation')('next');
  assert.equal(h.elements.get('navigateConfirm').hidden, true);
  assert.equal(JSON.parse(h.requests[0].options.body).resolution, 'ask');
  h.app.state.dirty = true; assert.equal(h.globalEvent('beforeunload').prevented, true);
  h.app.state.dirty = false; h.app.navigationPending = true;
  assert.equal(h.globalEvent('beforeunload').prevented, true);
});
