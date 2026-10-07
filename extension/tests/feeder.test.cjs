const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function background(responses, overrides = {}, workerId = 'primary', item = {uid: '27-1'}) {
  let now = 1800000000000;
  const calls = [], storage = { enabled: true, agentUrl: 'http://agent', token: 'secret', workerId };
  const noop = () => {};
  const event = { addListener: noop };
  const context = vm.createContext({
    console, AbortSignal, item, Date: class extends Date { static now() { return now; } },
    setTimeout: (callback, ms) => {
      if (ms < 25000) { now += ms; queueMicrotask(callback); }
      return 1;
    },
    clearTimeout: noop,
    chrome: {
      storage: { local: {
        get: async () => structuredClone(storage),
        set: async (patch) => Object.assign(storage, structuredClone(patch)),
      } },
      alarms: { create: noop, onAlarm: event },
      runtime: { onStartup: event, onInstalled: event, onMessage: event,
                 getManifest: () => ({ version: '1.5.1' }) },
      tabs: { sendMessage: async () => { calls.push('mut.gg'); return responses.shift(); } },
    },
    fetch: async (url, options) => {
      const endpoint = new URL(url).pathname;
      const body = JSON.parse(options.body || '{}');
      calls.push({ endpoint, body, version: options.headers['X-Feeder-Version'], workerId: options.headers['X-Feeder-Id'] });
      const defaults = {
        '/request-permit': { allowed: true, blocked: false, wait_ms: 0, rpm: 32 },
        '/request-result': { wait_ms: body.status === 429 ? 7200000 : 0 },
        '/ingest': { ok: true },
      };
      const data = endpoint in overrides ? overrides[endpoint] : defaults[endpoint];
      if (data instanceof Error) throw data;
      return { ok: true, json: async () => typeof data === 'function' ? await data() : data };
    },
  });
  vm.runInContext(readFileSync(path.join(__dirname, '../background.js'), 'utf8'), context);
  return { calls, storage, run: () => vm.runInContext("checkOne(1, item, {platform: 'pc'})", context) };
}

test('every refresh retry obtains a permit and reports its result before ingest', async () => {
  const fixture = background([{ status: 200, data: { updating: true } }, { status: 200, data: { pricesData: {} } }]);
  assert.equal((await fixture.run()).kind, 'ok');
  assert.deepEqual(fixture.calls.map(c => c.endpoint || c), [
    '/request-permit', 'mut.gg', '/request-result', '/request-permit', 'mut.gg', '/request-result', '/ingest',
  ]);
  assert.equal(fixture.storage.stats.requests, 2);
  assert.equal(fixture.storage.stats.checks, 1);
  assert.ok(fixture.calls.filter(c => c.endpoint).every(c => c.version === '1.5.1' && c.workerId === 'primary'));
});

test('exhausted updating responses never count as fresh checks', async () => {
  const fixture = background(Array.from({ length: 4 }, () => ({ status: 200, data: { updating: true } })));
  assert.equal((await fixture.run()).kind, 'pending');
  assert.equal(fixture.calls.filter(c => c === 'mut.gg').length, 4);
  assert.equal(fixture.calls.some(c => c.endpoint === '/ingest'), false);
  assert.equal(fixture.storage.stats.checks, 0);
});

test('429 forwards Retry-After and preserves a cooldown longer than 15 minutes', async () => {
  const fixture = background([{ status: 429, retry_after: '7200' }]);
  const result = await fixture.run();
  assert.equal(result.kind, 'blocked');
  assert.equal(result.wait_ms, 7200000);
  assert.equal(fixture.calls.find(c => c.endpoint === '/request-result').body.retry_after, '7200');
});

test('shared cooldown prevents a browser request', async () => {
  const fixture = background([], { '/request-permit': { allowed: false, blocked: true, wait_ms: 60000 } });
  assert.equal((await fixture.run()).kind, 'blocked');
  assert.equal(fixture.calls.includes('mut.gg'), false);
});

test('secondary profile tags every request and reports timeout separately without ingesting', async () => {
  const fixture = background([{status: -1}], {}, 'secondary');
  assert.equal((await fixture.run()).kind, 'timeout');
  assert.ok(fixture.calls.filter(c => c.endpoint).every(c => c.workerId === 'secondary'));
  const feedback = fixture.calls.find(c => c.endpoint === '/request-result').body;
  assert.equal(feedback.status, 0);
  assert.equal(feedback.outcome, 'timeout');
  assert.equal(fixture.calls.some(c => c.endpoint === '/ingest'), false);
});

test('a broken permit service fails closed', async () => {
  const fixture = background([], { '/request-permit': new Error('agent unavailable') });
  await assert.rejects(fixture.run(), /agent unavailable/);
  assert.equal(fixture.calls.includes('mut.gg'), false);
});

test('content script makes exactly one request even while prices are updating', async () => {
  let requests = 0;
  const context = vm.createContext({
    AbortSignal,
    chrome: { runtime: { onMessage: { addListener: () => {} } } },
    fetch: async (_url, options) => {
      requests++;
      assert.equal(options.cache, 'no-store');
      return { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ data: { updating: true } }) };
    },
  });
  vm.runInContext(readFileSync(path.join(__dirname, '../content.js'), 'utf8'), context);
  assert.equal((await vm.runInContext("fetchPrices('27-1', 'pc')", context)).data.updating, true);
  assert.equal(requests, 1);
});

test('failure diagnostics preserve actual HTTP status without reading response bodies', async () => {
  for (const [status, type, challenge] of [[200, 'text/html', true], [403, 'application/json', false]]) {
    const context = vm.createContext({
      AbortSignal,
      chrome: { runtime: { onMessage: { addListener: () => {} } } },
      fetch: async () => ({ status, ok: status === 200, redirected: true,
        headers: { get: name => ({ 'content-type': type, 'cf-mitigated': challenge ? 'challenge' : null })[name] },
        text: () => { throw new Error('must not read private response body'); },
      }),
    });
    vm.runInContext(readFileSync(path.join(__dirname, '../content.js'), 'utf8'), context);
    const result = await vm.runInContext("fetchPrices('27-1', 'pc')", context);
    assert.equal(result.status, 403);
    assert.equal(result.http_status, status);
    assert.equal(result.response_kind, type.split('/')[1]);
    assert.equal(result.challenged, challenge);
    assert.equal(result.redirected, true);
  }
});

test('stored failure metadata excludes raw details and credentials, while keeping refusal cooldown', async () => {
  const fixture = background([{ status: 429, http_status: 429, response_kind: 'html', challenged: true,
    detail: 'private body', token: 'private token', redirected: false }]);
  const result = await fixture.run();
  assert.equal(result.kind, 'blocked');
  assert.equal(result.wait_ms, 7200000);
  assert.deepEqual(fixture.storage.lastFailure, {
    at: 1800000000000, uid: '27-1', status: 429, httpStatus: 429,
    responseKind: 'html', challenged: true, redirected: false,
  });
});


test('permit waiters are FIFO so concurrent checks cannot overtake a waiting card', async () => {
  let release;
  const firstWait = new Promise(resolve => { release = resolve; });
  let permits = 0;
  const fixture = background([
    {status: 200, data: {pricesData: {}}}, {status: 200, data: {pricesData: {}}}
  ], {'/request-permit': async () => {
    if (++permits === 1) await firstWait;
    return {allowed: true, blocked: false, wait_ms: 0, rpm: 32};
  }});
  const first = fixture.run(), second = fixture.run();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(permits, 1);
  release();
  const results = await Promise.all([first, second]);
  assert.deepEqual(results.map(r => r.kind), ['ok', 'ok']);
  assert.equal(permits, 2);
});


test('HTTP 200 API errors and missing data cannot count as completed price scans', async () => {
  for (const body of [{error: 'private error'}, {data: null}, {data: []}, {data: {}},
                      {data: {pricesData: []}}, {errors: ['private'], data: {pricesData: {}}}]) {
    const context = vm.createContext({AbortSignal,
      chrome: {runtime: {onMessage: {addListener: () => {}}}},
      fetch: async () => ({ok: true, status: 200, headers: {get: () => 'application/json'}, json: async () => body})});
    vm.runInContext(readFileSync(path.join(__dirname, '../content.js'), 'utf8'), context);
    const result = await vm.runInContext("fetchPrices('27-1', 'pc')", context);
    assert.equal(result.outcome, 'api_error');
    assert.equal(result.status, 0);
    assert.equal(result.http_status, 200);
    assert.equal(result.data, undefined);
    assert.equal(JSON.stringify(result).includes('private'), false);
    const fixture = background([result]);
    await fixture.run();
    const feedback = fixture.calls.find(c => c.endpoint === '/request-result').body;
    assert.equal(feedback.outcome, 'api_error');
    assert.equal(feedback.uid, '27-1');
    assert.equal(fixture.calls.some(c => c.endpoint === '/ingest'), false);
  }
});

test('refresh telemetry identifies card without sharing the price payload', async () => {
  const fixture = background([{status:200,data:{updating:true}}, {status:200,data:{pricesData:{}}}]);
  await fixture.run();
  const reports=fixture.calls.filter(c=>c.endpoint==='/request-result').map(c=>c.body);
  assert.deepEqual(reports.map(r=>r.refreshing),[true,false]);
  assert.ok(reports.every(r=>r.uid==='27-1' && !('data' in r)));
});


test('known stuck cards use one permitted probe and recover when data is ready', async () => {
  const pending = background([{status:200,data:{updating:true}}], {}, 'primary', {uid:'27-1',refresh_retries:0});
  assert.equal((await pending.run()).kind,'pending');
  assert.equal(pending.calls.filter(c=>c==='mut.gg').length,1);
  assert.equal(pending.calls.some(c=>c.endpoint==='/ingest'),false);
  const ready = background([{status:200,data:{pricesData:{}}}], {}, 'primary', {uid:'27-1',refresh_retries:0});
  assert.equal((await ready.run()).kind,'ok');
  assert.equal(ready.storage.stats.checks,1);
});
