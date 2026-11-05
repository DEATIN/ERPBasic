'use strict';
// Проверки контракта интерфейса используют явную HTTP-фикстуру. Они не подтверждают учёт ERPNext и транзакции СУБД.
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const fs = require('node:fs/promises');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const screenshots = process.env.ERP_BASIC_SCREENSHOTS;
async function capture(page, filename) { if (screenshots) { await fs.mkdir(screenshots, { recursive: true }); await page.screenshot({ path: path.join(screenshots, filename), fullPage: true }); } }
let browser, server, origin;
const capability = { read: true, create: true, write: true, submit: true, cancel: true };
const kinds = ['quotes', 'orders', 'deliveries', 'invoices', 'purchaseOrders', 'receipts', 'purchaseInvoices', 'stockDocs', 'inventory', 'payments', 'items', 'customers', 'suppliers', 'warehouses'];
const session = { authenticated: true, business_date: '2026-11-05', time_zone: 'Europe/Moscow', user: 'employee@example.test', full_name: 'Сотрудник', csrf_token: 'csrf-test', companies: [{ name: 'Organisation A', default_currency: 'RUB' }, { name: 'Organisation B', default_currency: 'RUB' }], roles: ['ERP Basic User'], capabilities: Object.fromEntries(kinds.map(kind => [kind, capability])) };
const order = { name: 'SO-A-0001', modified: '2026-11-05 12:00:00.000001', docstatus: 0, customer: 'CUSTOMER-A', transaction_date: '2026-11-05', delivery_date: '2026-11-06', currency: 'RUB', net_total: 300, grand_total: 300, items: [{ name: 'row-1', item_code: 'SKU-A', qty: 3, rate: 100, uom: 'Nos', conversion_factor: 1, warehouse: 'Main - A' }] };
function envelope(kind, document, company = 'Organisation A') { return { kind, company, document, permissions: capability }; }
async function fulfill(route, payload, status = 200) { await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ message: payload }) }); }
function query(route) { return Object.fromEntries(new URL(route.request().url()).searchParams); }
async function pageFixture(handle) {
  const page = await browser.newPage({ viewport: { width: 1365, height: 900 } });
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/method/**', async route => {
    const method = new URL(route.request().url()).pathname.split('.').at(-1).split('/').at(-1);
    if (await handle(route, method) === true) return;
    if (method === 'session') return fulfill(route, session);
    if (method === 'list_documents') return fulfill(route, { rows: [], start: Number(query(route).start || 0), page_length: 25, has_more: false });
    return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ exc_type: 'DoesNotExistError' }) });
  });
  return { page, errors };
}
test.before(async () => {
  server = http.createServer(async (req, res) => {
    try {
      const requested = new URL(req.url, 'http://localhost').pathname;
      const filename = requested === '/erp' ? path.join(root, 'erp_basic/www/erp.html') : requested.startsWith('/assets/erp_basic/') ? path.join(root, 'erp_basic/public', path.basename(requested)) : '';
      if (!filename) { res.writeHead(404); res.end(); return; }
      const content = await fs.readFile(filename); res.setHeader('Content-Type', filename.endsWith('.js') ? 'application/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html'); res.end(content);
    } catch (_) { res.writeHead(404); res.end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve)); origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined, args: ['--no-sandbox'] });
});
test.after(async () => { if (browser) await browser.close(); if (server) await new Promise(resolve => server.close(resolve)); });

test('Интерфейс: создание, подтверждённая запись, проведение и связанная отгрузка', async () => {
  const writes = []; let releaseSave;
  const saveGate = new Promise(resolve => { releaseSave = resolve; });
  const { page, errors } = await pageFixture(async (route, method) => {
    if (method === 'save_document') {
      const args = route.request().postDataJSON(); writes.push(args); await saveGate;
      assert.equal(route.request().headers()['x-frappe-csrf-token'], 'csrf-test');
      await fulfill(route, envelope(args.kind, { ...order, ...args.document, name: 'SO-A-0001', modified: order.modified, grand_total: 300 })); return true;
    }
    if (method === 'submit_document') { const args = route.request().postDataJSON(); writes.push(args); await fulfill(route, envelope('orders', { ...order, docstatus: 1, modified: '2026-11-05 12:01:00.000001' })); return true; }
    if (method === 'create_related') { const args = route.request().postDataJSON(); writes.push(args); await fulfill(route, envelope('deliveries', { name: 'DN-A-0001', modified: order.modified, docstatus: 0, customer: order.customer, posting_date: '2026-11-05', items: [{ ...order.items[0], name: 'dn-row-1', against_sales_order: order.name, so_detail: 'row-1' }] })); return true; }
    return false;
  });
  try {
    await page.goto(`${origin}/erp?kind=orders`);
    await page.getByRole('button', { name: 'Создать', exact: true }).click();
    await page.locator('#field-customer').fill('CUSTOMER-A');
    await page.locator('#field-delivery_date').fill('2026-11-06');
    await page.locator('#field-items-0-item_code').fill('SKU-A');
    await page.locator('#field-items-0-qty').fill('3');
    await page.locator('#field-items-0-rate').fill('100');
    await page.locator('#field-items-0-warehouse').fill('Main - A');
    await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
    await page.getByText('Ожидаем подтверждение сервера…', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Провести', exact: true }).count(), 0);
    assert.equal(writes[0].document.name, undefined); assert.equal(writes[0].document.grand_total, undefined); assert.equal(writes[0].document.docstatus, undefined);
    assert.equal(writes[0].document.company, undefined); assert.equal(writes[0].company, 'Organisation A');
    releaseSave();
    await page.getByRole('button', { name: 'Провести', exact: true }).waitFor();
    assert.equal(await page.locator('.top-title strong').textContent(), 'SO-A-0001');
    page.on('dialog', dialog => dialog.accept());
    await page.getByRole('button', { name: 'Провести', exact: true }).click();
    await page.getByText('Проведён', { exact: true }).waitFor();
    assert.equal(writes[1].expected_modified, order.modified);
    await page.getByRole('button', { name: 'Отгрузка', exact: true }).click();
    await page.getByRole('button', { name: 'Провести', exact: true }).waitFor();
    assert.equal(await page.locator('.top-title strong').textContent(), 'DN-A-0001');
    assert.equal(writes[2].target_kind, 'deliveries');
    assert.equal(await page.locator('#field-items-0-qty').inputValue(), '3');
    assert.deepEqual(errors, []);
    await capture(page, 'server-ui-desktop.png');
    await page.getByRole('button', { name: 'Сменить тему', exact: true }).click();
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    await capture(page, 'server-ui-dark.png');
    const storage = await page.evaluate(() => Object.keys(localStorage));
    assert.deepEqual(storage, ['erpbasic-theme']);
  } finally { releaseSave(); await page.close(); }
});

test('Интерфейс: потеря ответа блокирует редактор до подтверждения точного повтора', async () => {
  const writes = [];
  const { page, errors } = await pageFixture(async (route, method) => {
    if (method === 'get_document') { await fulfill(route, envelope('orders', order)); return true; }
    if (method === 'save_document') {
      writes.push(route.request().postDataJSON());
      if (writes.length === 1) await route.abort('connectionfailed');
      else await fulfill(route, envelope('orders', { ...order, items: writes[1].document.items, modified: '2026-11-05 12:02:00.000001' }));
      return true;
    }
    return false;
  });
  try {
    await page.goto(`${origin}/erp?kind=orders&name=SO-A-0001`);
    await page.locator('#field-items-0-qty').fill('2');
    await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
    await page.getByText('Результат операции пока не подтверждён.', { exact: true }).waitFor();
    assert.equal(await page.locator('#field-items-0-qty').isDisabled(), true);
    assert.equal(await page.getByRole('button', { name: 'Сохранить', exact: true }).isDisabled(), true);
    await page.getByRole('button', { name: 'Повторить тот же запрос', exact: true }).click();
    await page.getByText('Документ сохранён на сервере.', { exact: true }).waitFor();
    assert.deepEqual(writes[0], writes[1]); assert.equal(writes.length, 2);
    assert.equal(await page.locator('#field-items-0-qty').inputValue(), '2');
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('Интерфейс: смена организации исключает запоздалые списки и использует пагинацию', async () => {
  let finishOld; let requestedOld;
  const pendingOld = new Promise(resolve => { finishOld = resolve; });
  const sawOld = new Promise(resolve => { requestedOld = resolve; });
  const listQueries = [];
  const { page, errors } = await pageFixture(async (route, method) => {
    if (method !== 'list_documents') return false;
    const args = query(route); listQueries.push(args);
    if (args.company === 'Organisation A') { requestedOld(); await pendingOld; }
    await fulfill(route, { rows: [{ name: args.company === 'Organisation A' ? 'OLD-A' : Number(args.start) ? 'B-SECOND-PAGE' : 'B-FIRST-PAGE', modified: '2026-11-05', docstatus: 0 }], start: Number(args.start), page_length: Number(args.page_length), has_more: Number(args.start) === 0 });
    return true;
  });
  try {
    await page.goto(`${origin}/erp?kind=orders`); await sawOld;
    await page.locator('#server-company').selectOption('Organisation B');
    await page.getByRole('link', { name: 'B-FIRST-PAGE', exact: true }).waitFor();
    finishOld();
    await page.getByRole('button', { name: 'Далее', exact: true }).click();
    await page.getByRole('link', { name: 'B-SECOND-PAGE', exact: true }).waitFor();
    assert.equal(await page.getByText('OLD-A', { exact: true }).count(), 0);
    assert.equal(listQueries.at(-1).start, '25'); assert.equal(listQueries.at(-1).page_length, '25');
    await page.setViewportSize({ width: 390, height: 844 });
    await capture(page, 'server-ui-mobile.png');
    assert.deepEqual(errors, []);
  } finally { finishOld(); await page.close(); }
});

test('Интерфейс: недоступность API показана явно и не создаёт демоданные', async () => {
  const { page, errors } = await pageFixture(async route => { await route.abort('connectionfailed'); return true; });
  try {
    await page.goto(`${origin}/erp`);
    await page.getByRole('button', { name: 'Повторить подключение', exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Создать', exact: true }).count(), 0);
    assert.deepEqual(await page.evaluate(() => Object.keys(localStorage)), []);
    assert.equal(await page.locator('script[src*="assets/app.js"]').count(), 0);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('Интерфейс: после полной перезагрузки новый документ восстанавливается тем же запросом', async () => {
  const writes = [];
  const { page, errors } = await pageFixture(async (route, method) => {
    if (method !== 'save_document') return false;
    writes.push(route.request().postDataJSON());
    if (writes.length === 1) await route.abort('connectionfailed');
    else await fulfill(route, envelope('orders', { ...order, ...writes[1].document, name: 'SO-RECOVERED', modified: order.modified }));
    return true;
  });
  try {
    await page.goto(`${origin}/erp?kind=orders`);
    await page.getByRole('button', { name: 'Создать', exact: true }).click();
    await page.locator('#field-customer').fill('CUSTOMER-A');
    await page.locator('#field-delivery_date').fill('2026-11-06');
    await page.locator('#field-items-0-item_code').fill('SKU-A');
    await page.locator('#field-items-0-rate').fill('100');
    await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
    await page.getByText('Результат операции пока не подтверждён.', { exact: true }).waitFor();
    const pending = await page.evaluate(() => JSON.parse(sessionStorage.getItem('erp-basic:pending-command:v1')));
    assert.equal(pending.key, writes[0].idempotency_key);
    assert.equal(pending.user, session.user); assert.equal(pending.company, 'Organisation A');
    assert.equal(JSON.stringify(pending).includes('csrf-test'), false);
    page.on('dialog', dialog => dialog.accept());
    await page.reload();
    await page.getByRole('heading', { name: 'Восстановление запроса' }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Создать', exact: true }).count(), 0);
    assert.equal(await page.locator('#server-document-form').count(), 0);
    await capture(page, 'recovery-desktop-light.png');
    await page.getByRole('button', { name: 'Повторить тот же запрос', exact: true }).click();
    await page.getByText('Документ сохранён на сервере.', { exact: true }).waitFor();
    assert.deepEqual(writes[0], writes[1]);
    assert.equal(await page.locator('.top-title strong').textContent(), 'SO-RECOVERED');
    assert.equal(await page.evaluate(() => sessionStorage.getItem('erp-basic:pending-command:v1')), null);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('Интерфейс: выход при неопределённом результате предупреждает и очищает служебную запись', async () => {
  const confirmations = [];
  const { page, errors } = await pageFixture(async (route, method) => {
    if (method === 'get_document') { await fulfill(route, envelope('orders', order)); return true; }
    if (method === 'save_document') { await route.abort('connectionfailed'); return true; }
    if (method === 'logout') { await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }); return true; }
    return false;
  });
  try {
    await page.goto(`${origin}/erp?kind=orders&name=${order.name}`);
    await page.locator('#field-items-0-qty').fill('1');
    await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
    await page.getByText('Результат операции пока не подтверждён.', { exact: true }).waitFor();
    page.on('dialog', dialog => { confirmations.push(dialog.message()); return dialog.accept(); });
    await page.getByRole('button', { name: 'Выйти', exact: true }).click();
    await page.getByRole('button', { name: 'Войти', exact: true }).waitFor();
    assert.equal(confirmations.length, 1);
    assert.match(confirmations[0], /найдите созданный документ на сервере/);
    assert.equal(await page.evaluate(() => sessionStorage.getItem('erp-basic:pending-command:v1')), null);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('Интерфейс: журнал обмена подтверждает очередь отдельно от доставки и экранирует внешний текст', async () => {
  let retried = false;
  const malicious = '<img src=x onerror="window.__exchange_xss=1">';
  const { page, errors } = await pageFixture(async (route, method) => {
    if (method === 'session') { await fulfill(route, { ...session, roles: ['ERP Basic Integration'] }); return true; }
    if (method === 'journal') {
      await fulfill(route, { incoming: [], outgoing: [{ name: 'outbox-1', connector: malicious, message_id: 'message-1', status: retried ? 'Pending' : 'Retry', attempts: 2, modified: '2026-11-05 12:00:00', error_message: malicious, can_retry: !retried, action: 'retry' }], start: 0, page_length: 25, has_more: false, incoming_has_more: false, outgoing_has_more: false, incoming_total: 0, outgoing_total: 1, can_retry: true, can_manage_mappings: false }); return true;
    }
    if (method === 'retry_outbox') {
      assert.equal(route.request().method(), 'POST');
      assert.equal(route.request().headers()['x-frappe-csrf-token'], 'csrf-test');
      assert.deepEqual(route.request().postDataJSON(), { company: 'Organisation A', name: 'outbox-1' });
      retried = true; await fulfill(route, { name: 'outbox-1', status: 'Pending' }); return true;
    }
    return false;
  });
  try {
    await page.goto(`${origin}/erp?view=exchange`);
    await page.getByRole('button', { name: /Исходящие/ }).click();
    await page.getByRole('button', { name: 'Повторить', exact: true }).waitFor();
    assert.equal(await page.locator('img').count(), 0);
    assert.equal(await page.evaluate(() => window.__exchange_xss), undefined);
    await capture(page, 'exchange-desktop-light.png');
    page.on('dialog', dialog => dialog.accept());
    await page.getByRole('button', { name: 'Повторить', exact: true }).click();
    await page.getByText('Сообщение возвращено в очередь. Доставка пока не подтверждена.', { exact: true }).waitFor();
    await page.getByText('В очереди', { exact: true }).waitFor();
    assert.equal(await page.getByText('Отправлено', { exact: true }).count(), 0);
    await page.getByRole('button', { name: 'Сменить тему', exact: true }).click();
    await capture(page, 'exchange-desktop-dark.png');
    await page.setViewportSize({ width: 390, height: 844 });
    await capture(page, 'exchange-mobile-dark.png');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('Интерфейс: серверные сообщения и поля документа не исполняют HTML', async () => {
  const malicious = '\"><svg onload="window.__document_xss=1">';
  const { page, errors } = await pageFixture(async (route, method) => {
    if (method === 'get_document') { await fulfill(route, envelope('orders', { ...order, customer: malicious, status: malicious })); return true; }
    if (method === 'save_document') { await route.fulfill({ status: 417, contentType: 'application/json', body: JSON.stringify({ exc_type: 'ValidationError', _server_messages: JSON.stringify([JSON.stringify({ message: '<img src=x onerror="window.__error_xss=1">Отклонено сервером' })]) }) }); return true; }
    return false;
  });
  try {
    await page.goto(`${origin}/erp?kind=orders&name=${order.name}`);
    assert.equal(await page.locator('#field-customer').inputValue(), malicious);
    assert.equal(await page.locator('[onload],[onerror]').count(), 0);
    await page.locator('#field-customer').fill('CUSTOMER-A');
    await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
    await page.getByText('Отклонено сервером', { exact: true }).waitFor();
    assert.equal(await page.locator('img').count(), 0);
    assert.equal(await page.evaluate(() => window.__document_xss || window.__error_xss), undefined);
    assert.equal(await page.getByText('Документ сохранён на сервере.', { exact: true }).count(), 0);
    assert.equal(await page.evaluate(() => sessionStorage.getItem('erp-basic:pending-command:v1')), null);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('Интерфейс: подбор прайс-листа использует ограниченный lookup с серверной пагинацией', async () => {
  const queries = [];
  const { page, errors } = await pageFixture(async (route, method) => {
    if (method !== 'lookup') return false;
    const args = query(route); queries.push(args);
    await fulfill(route, { rows: [{ name: Number(args.start) ? 'ERP Basic Selling' : 'Price List A', currency: 'RUB', selling: 1 }], start: Number(args.start), page_length: 25, has_more: !Number(args.start) }); return true;
  });
  try {
    await page.goto(`${origin}/erp?kind=orders`);
    await page.getByRole('button', { name: 'Создать', exact: true }).click();
    await page.getByRole('button', { name: 'Выбрать: Прайс-лист', exact: true }).click();
    await page.locator('#server-dialog').getByRole('button', { name: 'Далее', exact: true }).click();
    await page.locator('#server-dialog').getByRole('button', { name: /ERP Basic Selling/ }).click();
    assert.equal(await page.locator('#field-selling_price_list').inputValue(), 'ERP Basic Selling');
    assert.equal(queries.at(-1).kind, 'priceLists'); assert.equal(queries.at(-1).start, '25');
    assert.equal(queries.at(-1).company, 'Organisation A');
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});
