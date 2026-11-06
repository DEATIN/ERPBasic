'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { Client, ApiError, PendingStore, RECOVERY_KEY } = require('../erp_basic/public/server-api.js');

const hash = async value => createHash('sha256').update(value).digest('hex');
class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.get(key) || null; }
  setItem(key, value) { this.values.set(key, value); }
  removeItem(key) { this.values.delete(key); }
}
const boundSession = { authenticated: true, user: 'tester@example.test', companies: ['Organisation A', 'Organisation B', 'A'].map(name => ({ name })) };
const response = (message, status = 200) => ({ status, ok: status >= 200 && status < 300, json: async () => ({ message }) });
const errorResponse = (status, data = {}) => ({ status, ok: false, json: async () => data });
function clientWith(queue) {
  const calls = []; let key = 0;
  const client = new Client({ storage: new MemoryStorage(), digest: hash, createKey: () => `operation-${++key}`, fetch: async (url, init) => {
    calls.push({ url, ...init, json: init.body ? JSON.parse(init.body) : undefined });
    const next = queue.shift(); if (next instanceof Error) throw next;
    return typeof next === 'function' ? next(url, init) : next;
  }});
  client.bindSession(boundSession);
  return { client, calls };
}
const save = { kind: 'orders', company: 'Organisation A', document: { customer: 'CUSTOMER-A', items: [{ item_code: 'SKU-A', qty: 1, rate: 3 }] } };

test('GET использует свой origin, кодирует имя документа и передаёт сессию', async () => {
  const { client, calls } = clientWith([response({ name: 'SO / A & B' })]);
  await client.request('get_document', { kind: 'orders', company: 'Организация', name: 'SO / A & B' });
  const call = calls[0]; const url = new URL(call.url, 'https://example.test');
  assert.equal(url.pathname, '/api/method/erp_basic.api.get_document');
  assert.equal(url.searchParams.get('name'), 'SO / A & B');
  assert.equal(url.searchParams.get('company'), 'Организация');
  assert.equal(call.credentials, 'same-origin'); assert.equal(call.cache, 'no-store'); assert.equal(call.method, 'GET');
  assert.equal(call.body, undefined);
});

test('Сессия задаёт CSRF-токен для записи с ключом операции', async () => {
  const { client, calls } = clientWith([response({ ...boundSession, csrf_token: 'session-token' }), response({ document: { name: 'SO-1' } })]);
  await client.session(); await client.command('save_document', save);
  assert.equal(calls[1].headers['X-Frappe-CSRF-Token'], 'session-token');
  assert.equal(calls[1].headers['Content-Type'], 'application/json');
  assert.equal(calls[1].json.idempotency_key, 'operation-1');
  assert.deepEqual(calls[1].json.document, save.document);
  assert.equal(client.pending.size, 0);
});

test('Потеря связи сохраняет ключ и точный запрос для повтора', async () => {
  const { client, calls } = clientWith([new TypeError('network failure'), response({ document: { name: 'SO-1' } })]);
  await assert.rejects(client.command('save_document', save), error => error.uncertain && error.code === 'NETWORK_ERROR');
  assert.equal(client.pending.size, 1);
  await client.command('save_document', structuredClone(save));
  assert.equal(calls[0].json.idempotency_key, calls[1].json.idempotency_key);
  assert.equal(calls[0].body, calls[1].body); assert.equal(client.pending.size, 0);
});

test('Ошибка сервера оставляет результат неопределённым и не означает сохранение', async () => {
  const { client, calls } = clientWith([errorResponse(502), response({ saved: true })]);
  await assert.rejects(client.command('submit_document', { kind: 'orders', name: 'SO-1', company: 'A', expected_modified: 'v1' }), error => error.uncertain && error.status === 502);
  await client.command('submit_document', { expected_modified: 'v1', company: 'A', name: 'SO-1', kind: 'orders' });
  assert.equal(calls[0].json.idempotency_key, calls[1].json.idempotency_key);
});

test('Отказ авторизации после потери ответа не удаляет исходный ключ операции', async () => {
  const { client, calls } = clientWith([new Error('lost response'), errorResponse(401, { exc_type: 'AuthenticationError' }), response({ saved: true })]);
  await assert.rejects(client.command('save_document', save), error => error.uncertain);
  await assert.rejects(client.command('save_document', save), error => error.uncertain && error.status === 401);
  assert.equal(client.pending.size, 1);
  await client.command('save_document', save);
  assert.deepEqual(calls.map(call => call.json.idempotency_key), ['operation-1', 'operation-1', 'operation-1']);
});

test('Однозначная ошибка валидации разрешает исправленный запрос с новым ключом', async () => {
  const { client, calls } = clientWith([errorResponse(417, { exc_type: 'ValidationError', _server_messages: JSON.stringify([JSON.stringify({ message: '<b>Missing warehouse</b>' })]) }), response({ saved: true })]);
  await assert.rejects(client.command('save_document', save), error => !error.uncertain && error.message === '<b>Missing warehouse</b>');
  assert.equal(client.pending.size, 0);
  await client.command('save_document', { ...save, document: { ...save.document, items: [{ ...save.document.items[0], warehouse: 'Main - A' }] } });
  assert.notEqual(calls[0].json.idempotency_key, calls[1].json.idempotency_key);
});

test('Диагностика передаётся ограниченным обычным текстом без попытки разбирать HTML', async () => {
  const messages = ['<scr<script>ipt>Отклонено</scr<script>ipt>', '<img src=x onerror="alert(1)"', '<b>Ошибка</b> & "значение"', 'Ошибка '.repeat(150)];
  for (const message of messages) {
    for (const data of [
      { exc_type: 'ValidationError', _server_messages: JSON.stringify([JSON.stringify({ message })]) },
      { error: { code: 'ValidationError', message } },
      { message: { error: { code: 'ValidationError', message } } },
    ]) {
      const { client } = clientWith([errorResponse(417, data)]);
      await assert.rejects(client.request('get_document', { kind: 'orders', company: 'Organisation A', name: 'SO-1' }), error => error.message === message.slice(0, 800));
    }
  }
});

test('Конфликт версии отклоняет операцию и возвращается редактору', async () => {
  const { client } = clientWith([errorResponse(409, { exc_type: 'TimestampMismatchError' })]);
  await assert.rejects(client.command('submit_document', { kind: 'orders', company: 'Organisation A', name: 'SO-1', expected_modified: 'old' }), error => !error.uncertain && error.code === 'TimestampMismatchError');
  assert.equal(client.pending.size, 0);
});

test('Повреждённый или пустой ответ записи сохраняет запрос для проверки', async () => {
  for (const data of [null, [], {}, 'not a result']) {
    const { client } = clientWith([{ status: 200, ok: true, json: async () => data }]);
    await assert.rejects(client.command('save_document', save), error => error.uncertain && error.code === 'INVALID_RESPONSE');
    assert.equal(client.pending.size, 1);
  }
});

test('Неверная организация в ответе сохраняет ключ до подтверждения документа', async () => {
  const { client, calls } = clientWith([response({ company: 'wrong-company' }), response({ company: 'Organisation A', document: { name: 'SO-1' } })]);
  const validate = result => { if (result.company !== 'Organisation A') throw new ApiError('Wrong document company', { uncertain: true }); };
  await assert.rejects(client.command('save_document', save, { validate }), error => error.uncertain);
  await client.command('save_document', save, { validate });
  assert.equal(calls[0].json.idempotency_key, calls[1].json.idempotency_key);
});

test('Незавершённая команда блокирует запись другого содержимого', async () => {
  const { client, calls } = clientWith([new Error('lost')]);
  await assert.rejects(client.command('save_document', save));
  await assert.rejects(client.command('save_document', { ...save, company: 'Organisation B' }), error => error.code === 'RECOVERY_REQUIRED');
  assert.equal(calls.length, 1); assert.equal(client.pending.size, 1);
});

test('Таймаут прерывает запрос и оставляет результат записи неопределённым', async () => {
  const client = new Client({ storage: new MemoryStorage(), digest: hash, timeout: 10, createKey: () => 'operation-1', fetch: (_url, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener('abort', () => reject(Object.assign(new Error('abort'), { name: 'AbortError' })));
  }) });
  client.bindSession(boundSession);
  await assert.rejects(client.command('save_document', save), error => error.code === 'TIMEOUT' && error.uncertain);
  assert.equal(client.pending.size, 1);
});

test('Произвольные пути и неизвестные методы не обходят пространство API', async () => {
  const { client, calls } = clientWith([]);
  await assert.rejects(client.request('../anything'));
  await assert.rejects(client.request('get_document', {}, { builtin: true }));
  assert.equal(calls.length, 0);
});

test('Запрос записан в sessionStorage до POST и не содержит CSRF или пароля', async () => {
  const storage = new MemoryStorage();
  const client = new Client({ storage, digest: hash, createKey: () => 'operation-1', fetch: async (_url, init) => {
    const record = JSON.parse(storage.getItem(RECOVERY_KEY));
    assert.equal(record.user, boundSession.user);
    assert.equal(record.company, save.company);
    assert.equal(record.key, JSON.parse(init.body).idempotency_key);
    assert.deepEqual(record.params, save);
    assert.equal(record.csrf_token, undefined); assert.equal(record.password, undefined);
    assert.equal(storage.getItem(RECOVERY_KEY).includes('csrf-private'), false);
    return response({ document: { name: 'SO-1' } });
  }});
  client.bindSession(boundSession); client.csrfToken = 'csrf-private';
  await client.command('save_document', save);
  assert.equal(storage.getItem(RECOVERY_KEY), null);
});

test('Новый экземпляр клиента после перезагрузки повторяет исходный ключ и содержимое', async () => {
  const storage = new MemoryStorage(); const calls = [];
  const first = new Client({ storage, digest: hash, createKey: () => 'operation-1', fetch: async (_url, init) => { calls.push(JSON.parse(init.body)); throw new Error('lost'); } });
  first.bindSession(boundSession);
  await assert.rejects(first.command('save_document', save));
  const second = new Client({ storage, digest: hash, createKey: () => 'must-not-create-a-new-key', fetch: async (_url, init) => { calls.push(JSON.parse(init.body)); return response({ document: { name: 'SO-1' } }); } });
  second.bindSession(boundSession);
  const restored = await second.recovery();
  assert.equal(restored.key, 'operation-1'); assert.deepEqual(restored.params, save);
  await second.command(restored.method, restored.params);
  assert.deepEqual(calls[0], calls[1]); assert.equal(storage.getItem(RECOVERY_KEY), null);
});

test('Смена пользователя очищает прежний запрос без раскрытия содержимого', async () => {
  const storage = new MemoryStorage(); const store = new PendingStore({ storage, digest: hash });
  await store.write(boundSession.user, 'save_document', save, 'operation-1');
  const client = new Client({ storage, digest: hash, fetch: async () => { throw new Error('Нельзя обращаться к сети'); } });
  client.bindSession({ ...boundSession, user: 'different@example.test' });
  assert.equal(await client.recovery(), null); assert.equal(storage.getItem(RECOVERY_KEY), null);
});

test('Потеря права на организацию блокирует восстановление и не удаляет запрос молча', async () => {
  const storage = new MemoryStorage(); const store = new PendingStore({ storage, digest: hash });
  await store.write(boundSession.user, 'save_document', save, 'operation-1');
  const client = new Client({ storage, digest: hash, fetch: async () => { throw new Error('Нельзя обращаться к сети'); } });
  client.bindSession({ ...boundSession, companies: [{ name: 'Organisation B' }] });
  await assert.rejects(client.recovery(), error => error.code === 'RECOVERY_ACCESS_CHANGED');
  assert.ok(storage.getItem(RECOVERY_KEY));
});

test('Повреждение payload или ключа обнаруживается до повторного POST', async () => {
  for (const part of ['params', 'key']) {
    const storage = new MemoryStorage(); const store = new PendingStore({ storage, digest: hash });
    await store.write(boundSession.user, 'save_document', save, 'operation-1');
    const record = JSON.parse(storage.getItem(RECOVERY_KEY));
    if (part === 'params') record.params.document.items[0].qty = 200;
    else record.key = 'operation-changed';
    storage.setItem(RECOVERY_KEY, JSON.stringify(record));
    let calls = 0;
    const client = new Client({ storage, digest: hash, fetch: async () => { calls += 1; return response({}); } });
    client.bindSession(boundSession);
    await assert.rejects(client.recovery(), error => error.code === 'RECOVERY_CORRUPT');
    await assert.rejects(client.command('save_document', save), error => error.code === 'RECOVERY_CORRUPT');
    assert.equal(calls, 0); assert.ok(storage.getItem(RECOVERY_KEY));
  }
});

test('Запрещённое или переполненное хранилище останавливает запись до обращения к серверу', async () => {
  for (const storage of [null, { getItem: () => null, setItem: () => { throw new Error('QuotaExceeded'); }, removeItem() {} }]) {
    let calls = 0;
    const client = new Client({ storage, digest: hash, createKey: () => 'operation-1', fetch: async () => { calls += 1; return response({}); } });
    client.bindSession(boundSession);
    await assert.rejects(client.command('save_document', save), error => error.code === 'RECOVERY_UNAVAILABLE' && !error.uncertain);
    assert.equal(calls, 0);
  }
});

test('Параллельный вызов клиента не перезаписывает незавершённый запрос', async () => {
  let release; let started;
  const wait = new Promise(resolve => { release = resolve; });
  const ready = new Promise(resolve => { started = resolve; });
  let calls = 0;
  const client = new Client({ storage: new MemoryStorage(), digest: hash, createKey: () => 'operation-1', fetch: async () => { calls += 1; started(); await wait; return response({ document: { name: 'SO-1' } }); } });
  client.bindSession(boundSession);
  const first = client.command('save_document', save);
  await ready;
  await assert.rejects(client.command('save_document', save), error => error.code === 'REQUEST_IN_PROGRESS');
  release(); await first; assert.equal(calls, 1);
});

test('Поля с реквизитами авторизации запрещены в служебной записи', async () => {
  const { client, calls } = clientWith([]);
  await assert.rejects(client.command('save_document', { ...save, document: { ...save.document, api_secret: 'never-store' } }), error => error.code === 'INVALID_COMMAND');
  assert.equal(calls.length, 0); assert.equal(client.store.storage.getItem(RECOVERY_KEY), null);
});

test('Неавторизованный bootstrap сохраняет запрос скрытым до входа того же пользователя', async () => {
  const storage = new MemoryStorage(); const store = new PendingStore({ storage, digest: hash });
  await store.write(boundSession.user, 'save_document', save, 'operation-1');
  const client = new Client({ storage, digest: hash, fetch: async () => { throw new Error('Нельзя обращаться к сети'); } });
  client.bindSession({ authenticated: false });
  assert.equal(await client.recovery(), null); assert.ok(storage.getItem(RECOVERY_KEY));
  client.bindSession(boundSession);
  assert.equal((await client.recovery()).key, 'operation-1');
});

test('Штатный logout с пустым объектом очищает незавершённый запрос и данные сессии', async () => {
  const { client, calls } = clientWith([new Error('lost'), { status: 200, ok: true, json: async () => ({}) }]);
  await assert.rejects(client.command('save_document', save));
  await client.logout();
  assert.equal(calls[1].url, '/api/method/logout');
  assert.equal(client.store.storage.getItem(RECOVERY_KEY), null);
  assert.equal(client.user, ''); assert.equal(client.pending.size, 0);
});

test('Ошибки обмена внутри Frappe message отклоняются, а namespace ограничен', async () => {
  const { client, calls } = clientWith([response({ accepted: false, error: { code: 'MissingMapping', message: '<b>Нужно соответствие</b>' } }, 422)]);
  await assert.rejects(client.request('retry_inbox', { company: 'A', name: 'inbox-1' }, { namespace: 'integration', verb: 'POST', mutation: true }), error => error.code === 'MissingMapping' && error.message === '<b>Нужно соответствие</b>' && !error.uncertain);
  assert.equal(calls[0].url, '/api/method/erp_basic.integration.retry_inbox');
  await assert.rejects(client.request('set_mapping', {}, { namespace: 'integration' }));
  await assert.rejects(client.request('journal', {}, { namespace: '../other' }));
  assert.equal(calls.length, 1);
});

test('Frappe 403 для завершённой сессии распознаётся без повтора записи', async () => {
  const { client, calls } = clientWith([errorResponse(403, { exc_type: 'PermissionError' }), response({ authenticated: false })]);
  await assert.rejects(client.command('save_document', save), error => error.code === 'SessionExpired' && !error.uncertain);
  assert.equal(calls.length, 2); assert.equal(calls[1].url, '/api/method/erp_basic.api.session');
  assert.equal(calls[1].method, 'GET'); assert.equal(client.store.storage.getItem(RECOVERY_KEY), null);
});

test('Frappe 403 при действующей сессии остаётся ошибкой прав доступа', async () => {
  const { client, calls } = clientWith([errorResponse(403, { exc_type: 'PermissionError' }), response({ authenticated: true })]);
  await assert.rejects(client.request('get_document', { kind: 'orders', company: 'Organisation A', name: 'SO-1' }), error => error.code === 'PermissionError' && error.status === 403);
  assert.equal(calls.length, 2);
});
