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
    'summary-language', 'translate', 'source-note'].map(id => [id, {}]));
  const source = {value: 'mp4'};
  const file = {};
  const context = vm.createContext({source, file, $: id => elements[id]});
  const handler = script.slice(script.indexOf('function updateSource()'),
                               script.indexOf("file.addEventListener('change'"));
  vm.runInContext(handler + ';updateSource()', context);
  assert.equal(elements.url.disabled, true, 'invalid URL must not block MP4 submission');
  assert.equal(file.disabled, false);
  assert.equal(file.required, true);
  assert.equal(elements.summary.disabled, true);
  source.value = 'youtube';
  vm.runInContext('updateSource()', context);
  assert.equal(file.disabled, true, 'oversized MP4 validity must not block YouTube submission');
  assert.equal(elements.url.disabled, false);
  assert.equal(elements.url.required, true);
  assert.equal(elements.summary.disabled, false);
});
