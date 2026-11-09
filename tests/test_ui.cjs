// Exercise the shipped source-switch handler without a browser or DOM dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

test('the shipped script has valid JavaScript syntax', () => {
  new vm.Script(script);
});

test('switching sources excludes invalid inactive inputs from form validation', () => {
  const elements = Object.fromEntries(['url-input', 'file-input', 'url', 'summary',
    'summary-language', 'translate'].map(id => [id, {}]));
  const source = {value: 'mp4'};
  const file = {};
  const context = vm.createContext({source, file, $: id => elements[id]});
  const handler = script.slice(script.indexOf('function updateSource()'),
                               script.indexOf("file.addEventListener('change'"));
  vm.runInContext(handler + ';updateSource()', context);
  assert.equal(elements.url.disabled, true, 'invalid URL must not block MP4 submission');
  assert.equal(file.disabled, false);
  assert.equal(file.required, true);
  assert.equal(elements.summary.disabled, false);
  assert.match(script, /transcribing:'語音轉錄中'/);
  source.value = 'youtube';
  vm.runInContext('updateSource()', context);
  assert.equal(file.disabled, true, 'oversized MP4 validity must not block YouTube submission');
  assert.equal(elements.url.disabled, false);
  assert.equal(elements.url.required, true);
  assert.equal(elements.summary.disabled, false);
});

test('MP4 submission sends AI options and shows partial transcript warnings', async () => {
  const listeners = {};
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', style: {}, classList: {add() {}}, files: [{name: 'lecture.mp4'}],
      setAttribute() {}, setCustomValidity() {},
      addEventListener(event, callback) { listeners[`${id}:${event}`] = callback; },
    });
    return elements.get(id);
  };
  for (const [id, value] of Object.entries({source: 'mp4', threshold: '.32', gap: '4',
      max: '120', 'summary-language': 'zh-TW', translate: 'en'})) element(id).value = value;
  element('summary').checked = true;
  const requests = [];
  const context = vm.createContext({
    document: {getElementById: element}, FormData,
    requestAnimationFrame() {return 0;}, cancelAnimationFrame() {},
    fetch: async (url, options) => {
      if (!options) return {ok: false};
      requests.push({url, options});
      return {ok: true, json: async () => ({id: 'job', status: 'done', source: 'mp4',
        title: 'lecture.mp4', frames: 1, cues: 1, transcript_status: 'partial',
        warnings: ['來源逐字稿不完整；00:05:00–00:05:05']})};
    },
  });
  vm.runInContext(script, context);
  listeners['form:submit']({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests[0].url, '/api/jobs/upload');
  assert.equal(requests[0].options.body.get('summary'), 'true');
  assert.equal(requests[0].options.body.get('translate'), 'en');
  assert.match(element('result').textContent, /逐字稿不完整.*00:05:00–00:05:05/);
  assert.equal(element('download').href, '/api/jobs/job/download');
});
