'use strict';
// Проверка запуска и восстановления без DOM-движка. Верстку проверяет отдельный Chromium job.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { ApiError } = require('../erp_basic/public/server-api.js');
const source = fs.readFileSync(path.join(__dirname, '../erp_basic/public/server-app.js'), 'utf8');
const permission = { read: true, create: true, write: true, submit: true, cancel: true };
const session = { authenticated: true, business_date: '2026-11-05', time_zone: 'Europe/Moscow', user: 'employee@example.test', full_name: 'Сотрудник', roles: [], companies: [{ name: 'Организация А', default_currency: 'RUB' }], capabilities: { orders: permission } };
const pending = { method: 'save_document', key: 'operation-1', company: 'Организация А', created_at: '2026-11-05T12:00:00.000Z', params: { kind: 'orders', company: 'Организация А', document: { customer: 'Покупатель', items: [{ item_code: 'Товар', qty: '1', rate: '25.00' }] } } };

function element() { return { innerHTML: '', textContent: '', className: '', contains: () => true, addEventListener() {}, appendChild() {}, querySelector: () => null, focus() {}, close() {}, remove() {} }; }
async function bootstrap(options = {}) {
  const elements = new Map(['server-app', 'server-content', 'server-dialog', 'server-actions', 'toasts'].map(id => [id, element()]));
  const listeners = {}; const calls = [];
  class Client {
    async session() { if (options.sessionError) throw options.sessionError; return { ...session, ...options.session }; }
    async recovery() { calls.push(['recovery']); if (options.recoveryError) throw options.recoveryError; return options.pending || null; }
    async request(method, params) { calls.push([method, params]); return { rows: [], start: 0, page_length: 25, has_more: false }; }
    async command(method, params, configuration) {
      calls.push([method, params]);
      const result = { kind: 'orders', company: 'Организация А', document: { name: 'SO-CONFIRMED', modified: '2026-11-05 12:00:01', docstatus: 0, customer: 'Покупатель', items: [], grand_total: '25.00' }, permissions: permission };
      if (configuration.validate) configuration.validate(result);
      return result;
    }
  }
  const document = {
    getElementById: id => elements.get(id) || null,
    createElement: element,
    querySelector: () => null,
    addEventListener: (event, callback) => { listeners[event] = callback; },
  };
  const window = { ERPBasicAPI: { Client, ApiError }, history: { replaceState() {} }, addEventListener() {}, setTimeout: () => 0, confirm: () => true };
  const context = vm.createContext({ document, window, location: { search: '?kind=orders' }, URLSearchParams });
  vm.runInContext(source, context, { filename: 'server-app.js' });
  await new Promise(resolve => setImmediate(resolve));
  return { elements, calls, screen: () => elements.get('server-app').innerHTML + elements.get('server-content').innerHTML,
    click: action => listeners.click({ target: { closest: () => ({ dataset: { action }, tagName: 'BUTTON', disabled: false }) }, preventDefault() {} }) };
}

test('Запуск восстанавливает служебный запрос до загрузки списков и не выдаёт его за документ', async () => {
  const ui = await bootstrap({ pending });
  assert.match(ui.screen(), /Восстановление запроса/);
  assert.match(ui.screen(), /создание нового/);
  assert.doesNotMatch(ui.screen(), /server-document-form/);
  assert.deepEqual(ui.calls, [['recovery']]);
});

test('Повтор восстановленной команды заменяет экран только подтверждённым документом', async () => {
  const ui = await bootstrap({ pending });
  await ui.click('retry-command');
  assert.equal(ui.calls[1][0], 'save_document');
  assert.equal(JSON.stringify(ui.calls[1][1]), JSON.stringify(pending.params));
  assert.match(ui.screen(), /SO-CONFIRMED/);
  assert.match(ui.screen(), /server-document-form/);
  assert.doesNotMatch(ui.screen(), /Восстановление запроса/);
});

test('Повреждённое восстановление блокирует список и показывает явный выход', async () => {
  const ui = await bootstrap({ recoveryError: new ApiError('Повреждён служебный запрос.', { code: 'RECOVERY_CORRUPT' }) });
  assert.match(ui.screen(), /Повреждён служебный запрос/);
  assert.match(ui.screen(), /Новые записи заблокированы/);
  assert.doesNotMatch(ui.screen(), /data-action="retry-command"/);
  assert.deepEqual(ui.calls, [['recovery']]);
});

test('Обычный запуск читает страницу сервера после проверки отсутствия восстановления', async () => {
  const ui = await bootstrap();
  assert.equal(ui.calls[0][0], 'recovery');
  assert.equal(ui.calls[1][0], 'list_documents');
  assert.equal(ui.calls[1][1].page_length, 25);
  assert.match(ui.screen(), /Документов пока нет/);
});

test('Ошибка сессии оставляет явное состояние подключения без локального учёта', async () => {
  const ui = await bootstrap({ sessionError: new ApiError('Сервер недоступен.') });
  assert.match(ui.screen(), /Сервер недоступен/);
  assert.match(ui.screen(), /Повторить подключение/);
  assert.doesNotMatch(ui.screen(), /Создать/);
  assert.deepEqual(ui.calls, []);
});


test('Новый документ использует бизнес-дату сервера, а не часы браузера', async () => {
  const ui = await bootstrap({ session: { business_date: '2001-02-03' } });
  await ui.click('new');
  assert.match(ui.screen(), /id="field-transaction_date"[^>]*value="2001-02-03"/);
});
