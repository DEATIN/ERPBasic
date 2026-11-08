'use strict';
// Контракт браузерного транспорта/очереди. Реальную БД и внешние сервисы эти тесты не заменяют.
const test = require('node:test');
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { Client, ApiError, RECOVERY_KEY } = require('../erp_basic/public/server-api.js');
const { Workspace, ScanStore, QUEUE_KEY, MAX_QUEUE, validateJob, validatePage, esc } = require('../erp_basic/public/marking-app.js');
class Storage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.get(key) || null; }
  setItem(key, value) { this.values.set(key, value); }
  removeItem(key) { this.values.delete(key); }
}
const digest = async value => createHash('sha256').update(value).digest('hex');
const session = { authenticated: true, user: 'operator@example.test', companies: [{ name: 'A' }, { name: 'B' }] };
const companies = new Set(['A', 'B']);
const response = message => ({ status: 200, ok: true, json: async () => ({ message }) });
const raw = ']d2010001234567890521AbC001\x1d91TEST\x1d92proof';
const scan = { company: 'A', name: 'MARK-1', raw_code: raw, expected_revision: 5, device_id: 'device-000001', event_id: 'event-000001', sequence: 1 };
function makeClient(storage, fetch) {
  const client = new Client({ storage, digest, fetch, createKey: () => 'operation-000001' });
  client.bindSession(session); client.csrfToken = 'csrf-fixture'; return client;
}
function makeStore(storage = new Storage()) { let sequence = 0; return new ScanStore({ storage, createId: () => `fixture-id-${++sequence}` }); }

test('Маркировка: GET использует выделенный namespace и разрешённые методы', async () => {
  const calls = [];
  const client = makeClient(new Storage(), async (url, init) => { calls.push({ url, init }); return response({ jobs: [] }); });
  await client.request('workspace', { company: 'A & B', role: 'retail', start: 25, page_length: 25 }, { namespace: 'marking' });
  const url = new URL(calls[0].url, 'https://fixture.test');
  assert.equal(url.pathname, '/api/method/erp_basic.marking.api.workspace');
  assert.equal(url.searchParams.get('company'), 'A & B');
  assert.equal(url.searchParams.get('start'), '25');
  assert.equal(calls[0].init.credentials, 'same-origin');
  await assert.rejects(client.request('unknown_method', {}, { namespace: 'marking' }), { code: 'INVALID_METHOD' });
  await assert.rejects(client.request('workspace', {}, { namespace: 'marking.api' }), { code: 'INVALID_METHOD' });
  assert.equal(calls.length, 1);
});

test('Маркировка: команда сохраняет GS, регистр, ведущие нули, CSRF и версию', async () => {
  let request;
  const storage = new Storage();
  const client = makeClient(storage, async (url, init) => { request = { url, init, params: JSON.parse(init.body) }; return response({ job: { name: 'MARK-1' } }); });
  await client.command('marking_scan_code', scan);
  assert.equal(request.url, '/api/method/erp_basic.marking.api.scan_code');
  assert.equal(request.params.raw_code, raw); assert.equal(request.params.expected_revision, 5);
  assert.equal(request.params.device_id, scan.device_id); assert.equal(request.params.sequence, 1);
  assert.equal(request.params.idempotency_key, 'operation-000001');
  assert.equal(request.init.headers['X-Frappe-CSRF-Token'], 'csrf-fixture');
  assert.equal(storage.getItem(RECOVERY_KEY), null);
});

test('Маркировка: reload и потерянный ответ используют прежние байты и прежний ключ', async () => {
  const storage = new Storage(); const bodies = [];
  const first = makeClient(storage, async (_, init) => { bodies.push(init.body); throw new TypeError('lost response'); });
  await assert.rejects(first.command('marking_scan_code', scan), error => error.uncertain);
  const saved = JSON.parse(storage.getItem(RECOVERY_KEY));
  assert.equal(saved.params.raw_code, raw); assert.equal(saved.method, 'marking_scan_code');
  assert.ok(!storage.getItem(RECOVERY_KEY).includes('csrf-fixture'));
  const second = makeClient(storage, async (_, init) => { bodies.push(init.body); return response({ job: {} }); });
  const pending = await second.recovery();
  await assert.rejects(second.command('marking_complete_job', { company: 'A', name: 'MARK-1', expected_revision: 5 }), { code: 'RECOVERY_REQUIRED' });
  await second.command(pending.method, pending.params);
  assert.equal(bodies[0], bodies[1]); assert.equal(storage.getItem(RECOVERY_KEY), null);
});

test('Маркировка и ERP используют единственный журнал незавершённой команды', async () => {
  const storage = new Storage(); let calls = 0;
  const client = makeClient(storage, async () => { ++calls; throw new Error('network'); });
  await assert.rejects(client.command('marking_complete_job', { company: 'A', name: 'MARK-1', expected_revision: 5 }), error => error.uncertain);
  await assert.rejects(client.command('save_document', { company: 'A', kind: 'orders', document: {} }), { code: 'RECOVERY_REQUIRED' });
  assert.equal(calls, 1);
});

test('Маркировка: ОСУ и импорт ожидаемого состава проходят тем же защищённым транспортом', async () => {
  const calls = []; const storage = new Storage();
  const client = makeClient(storage, async (url, init) => { calls.push({ url, body: JSON.parse(init.body) }); return response({ job: {} }); });
  await client.command('marking_record_quantity', { company: 'A', name: 'M-1', gtin: '00012345678905', quantity: 2, expected_revision: 0 });
  await client.command('marking_import_expected_codes', { company: 'A', name: 'M-2', codes: ['010001234567890521Case001'], expected_revision: 1 });
  assert.equal(calls[0].url, '/api/method/erp_basic.marking.api.record_quantity');
  assert.equal(calls[0].body.gtin, '00012345678905'); assert.equal(calls[0].body.quantity, 2);
  assert.equal(calls[1].url, '/api/method/erp_basic.marking.api.import_expected_codes');
  assert.deepEqual(calls[1].body.codes, ['010001234567890521Case001']);
  assert.equal(storage.getItem(RECOVERY_KEY), null);
});

test('Маркировка: allowlist не пропускает секреты, произвольные поля и чужую Company', async () => {
  let requests = 0;
  const client = makeClient(new Storage(), async () => { ++requests; return response({}); });
  await assert.rejects(client.command('marking_create_job', { company: 'A', lines: [{ access_token: 'do-not-store' }] }), { code: 'INVALID_COMMAND' });
  await assert.rejects(client.command('marking_scan_code', { ...scan, ignored: true }), { code: 'INVALID_COMMAND' });
  await assert.rejects(client.command('marking_scan_code', { ...scan, company: 'C' }), { code: 'AuthenticationError' });
  await assert.rejects(client.command('marking_fake_success', scan), { code: 'INVALID_COMMAND' });
  assert.equal(requests, 0);
});

test('Очередь: сохраняет события до ACK, очищает КМ и ведёт sequence для каждого job', () => {
  const storage = new Storage(); const store = makeStore(storage);
  const first = store.enqueue(session.user, companies, 'A', 'MARK-1', raw);
  const second = store.enqueue(session.user, companies, 'A', 'MARK-1', 'second\x1dvalue');
  assert.equal(first.sequence, 1); assert.equal(second.sequence, 2); assert.equal(first.device_id, second.device_id);
  const reloaded = makeStore(storage).read(session.user, companies);
  assert.deepEqual(reloaded.queue.events.map(event => event.raw_code), [raw, 'second\x1dvalue']);
  assert.throws(() => store.acknowledge(session.user, companies, second.event_id), { code: 'SCAN_SEQUENCE_CONFLICT' });
  store.acknowledge(session.user, companies, first.event_id);
  assert.ok(!storage.getItem(QUEUE_KEY).includes('AbC001'));
  store.acknowledge(session.user, companies, second.event_id);
  assert.equal(store.read(session.user, companies).queue, null);
  const third = store.enqueue(session.user, companies, 'A', 'MARK-1', 'third');
  assert.equal(third.sequence, 3);
  store.acknowledge(session.user, companies, third.event_id);
  const other = store.enqueue(session.user, companies, 'B', 'MARK-2', 'other');
  assert.equal(other.sequence, 1);
  assert.ok(!storage.getItem(QUEUE_KEY).includes('raw_code":"third'));
});

test('Очередь: 1000 последовательных событий без потерь, с ограничением роста', () => {
  const store = makeStore();
  for (let index = 1; index <= MAX_QUEUE; index += 1) store.enqueue(session.user, companies, 'A', 'MARK-1', `scan-${index}`);
  const queue = store.read(session.user, companies).queue;
  assert.equal(queue.events.length, 1000);
  assert.equal(new Set(queue.events.map(event => event.event_id)).size, 1000);
  assert.deepEqual(queue.events.map(event => event.sequence), Array.from({ length: 1000 }, (_, i) => i + 1));
  assert.throws(() => store.enqueue(session.user, companies, 'A', 'MARK-1', 'overflow'), { code: 'SCAN_QUEUE_FULL' });
  assert.equal(store.read(session.user, companies).queue.events.length, 1000);
});

test('Очередь: смена Company и отозванный доступ не теряют данные и не раскрывают другому пользователю', () => {
  const store = makeStore();
  store.enqueue(session.user, companies, 'A', 'MARK-1', raw);
  assert.throws(() => store.enqueue(session.user, companies, 'B', 'MARK-2', 'other'), { code: 'RECOVERY_REQUIRED' });
  assert.throws(() => store.read(session.user, new Set(['B'])), { code: 'RECOVERY_ACCESS_CHANGED' });
  assert.equal(store.read(session.user, companies).queue.events[0].raw_code, raw);
  assert.equal(store.read('another@example.test', companies), null);
  assert.equal(store.storage.getItem(QUEUE_KEY), null);
});

test('Очередь: повреждённая последовательность или недоступное хранилище блокируют отправку', () => {
  const storage = new Storage(); const store = makeStore(storage);
  store.enqueue(session.user, companies, 'A', 'MARK-1', raw);
  const saved = JSON.parse(storage.getItem(QUEUE_KEY)); saved.queue.events[0].sequence = 2;
  storage.setItem(QUEUE_KEY, JSON.stringify(saved));
  assert.throws(() => store.read(session.user, companies), { code: 'RECOVERY_CORRUPT' });
  assert.throws(() => makeStore(null).enqueue(session.user, companies, 'A', 'MARK-1', raw), { code: 'RECOVERY_UNAVAILABLE' });
});

test('Ответы: неверная пагинация и другая организация не принимаются как успех', () => {
  const page = { jobs: [], start: 0, page_length: 25, has_more: false };
  assert.equal(validatePage(page), page);
  for (const invalid of [{ ...page, start: '25' }, { ...page, start: -1 }, { ...page, jobs: Array(101).fill({}) }, { ...page, has_more: 'false' }]) assert.throws(() => validatePage(invalid));
  const job = { name: 'M-1', company: 'A', role: 'retail', revision: 1, expected_quantity: 2, accepted_count: 0, rejected_count: 0, lines: [] };
  assert.equal(validateJob({ job }, 'A'), job);
  assert.throws(() => validateJob({ job }, 'B', 'M-1', true), error => error.uncertain && error.code === 'INVALID_MARKING_JOB');
  assert.throws(() => validateJob({ job: { ...job, revision: -1 } }, 'A'));
  assert.equal(esc('<img src=x onerror="alert(1)"> &'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp;');
});

test('Workspace сериализует очередь и подставляет подтверждённую revision каждому событию', async () => {
  let release; const gate = new Promise(resolve => { release = resolve; }); const writes = [];
  const job = { name: 'M-1', company: 'A', role: 'manufacturer', profile: 'test-pu-v1', mode: 'unit', status: 'Draft', revision: 0, expected_quantity: 3, accepted_count: 0, rejected_count: 0, lines: [{ gtin: '00012345678905', quantity: 3, accepted_quantity: 0 }] };
  const workspace = new Workspace({ company: 'A', session, api: { command: async (method, params, options) => {
    writes.push({ method, params: structuredClone(params) });
    if (writes.length === 1) await gate;
    const result = { job: { ...job, revision: writes.length, accepted_count: writes.length }, scan: { name: `S-${writes.length}`, result: 'Accepted' } };
    options.validate(result); return result;
  } } });
  workspace.store = makeStore(); workspace.state.job = job; workspace.state.permissions = { scan: true };
  workspace.enqueue(raw); workspace.enqueue('second'); workspace.enqueue('third');
  assert.equal(workspace.queueCount(), 3); assert.equal(writes.length, 1); assert.equal(workspace.locked(), true);
  release();
  for (let i = 0; i < 100 && workspace.draining; i += 1) await new Promise(resolve => setImmediate(resolve));
  assert.equal(workspace.queueCount(), 0); assert.equal(workspace.locked(), false);
  assert.deepEqual(writes.map(row => row.params.expected_revision), [0, 1, 2]);
  assert.deepEqual(writes.map(row => row.params.sequence), [1, 2, 3]);
  assert.equal(workspace.state.job.accepted_count, 3);
  assert.ok(!workspace.store.storage.getItem(QUEUE_KEY).includes('raw_code'));
});

test('Workspace после конфликта не заменяет event_id и ждёт явного обновления задания', async () => {
  const writes = []; let reads = 0;
  const job = { name: 'M-1', company: 'A', role: 'manufacturer', profile: 'test-pu-v1', mode: 'unit', status: 'Draft', revision: 0, expected_quantity: 1, accepted_count: 0, rejected_count: 0, lines: [{ gtin: '00012345678905', quantity: 1, accepted_quantity: 0 }] };
  const workspace = new Workspace({ company: 'A', session, api: {
    command: async (method, params, options) => {
      writes.push(structuredClone(params));
      if (writes.length === 1) throw new ApiError('Версия изменена', { status: 409, code: 'ConflictError' });
      const result = { job: { ...job, revision: 8, accepted_count: 1 }, scan: { name: 'S-1', result: 'Accepted' } }; options.validate(result); return result;
    },
    request: async () => { ++reads; return { job: { ...job, revision: 7 }, scans: [], history: [], start: 0, page_length: 25, has_more: false, permissions: { scan: true } }; },
  } });
  workspace.store = makeStore(); workspace.state.job = job; workspace.state.permissions = { scan: true };
  workspace.enqueue(raw);
  for (let i = 0; i < 100 && workspace.draining; i += 1) await new Promise(resolve => setImmediate(resolve));
  assert.equal(workspace.state.paused, true); assert.equal(workspace.queueCount(), 1); assert.equal(reads, 0);
  await workspace.resume();
  assert.equal(reads, 1); assert.equal(writes.length, 2); assert.equal(writes[0].event_id, writes[1].event_id);
  assert.equal(writes[0].expected_revision, 0); assert.equal(writes[1].expected_revision, 7);
  assert.equal(workspace.queueCount(), 0);
});

test('Workspace загружает точные строки основания и складские количества с сервера', async () => {
  const calls = [];
  const workspace = new Workspace({ company: 'A', session, api: { request: async (method, params) => {
    calls.push({ method, params });
    return { company: 'A', kind: 'receipts', document: { name: 'PR-1', docstatus: 0, items: [{ name: 'native-row-1', item_code: 'SKU-A', qty: 1, stock_qty: 12 }] } };
  } } });
  workspace.state.form.reference_kind = 'receipts'; workspace.state.form.reference_name = 'PR-1';
  await workspace.loadReference();
  assert.deepEqual(calls, [{ method: 'get_document', params: { company: 'A', kind: 'receipts', name: 'PR-1' } }]);
  assert.deepEqual(workspace.state.form.lines, [{ item_code: 'SKU-A', gtin: '', quantity: 12, reference_line: 'native-row-1' }]);
  assert.deepEqual(workspace.state.referenceLoaded, { kind: 'receipts', name: 'PR-1' });
});

test('Workspace отклоняет чужое основание и не заменяет введённые строки', async () => {
  const workspace = new Workspace({ company: 'A', session, api: { request: async () => ({ company: 'B', kind: 'receipts', document: { name: 'PR-1', items: [{ name: 'row', item_code: 'SKU', qty: 1 }] } }) } });
  workspace.state.form.reference_kind = 'receipts'; workspace.state.form.reference_name = 'PR-1';
  const previous = structuredClone(workspace.state.form.lines);
  await workspace.loadReference();
  assert.deepEqual(workspace.state.form.lines, previous); assert.equal(workspace.state.referenceLoaded, undefined);
  assert.match(workspace.state.error, /подтвердить доступное основание/);
});
