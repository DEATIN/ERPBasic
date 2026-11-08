'use strict';
// Chromium + явная HTTP-фикстура проверяют UI-контракт. Ни ГИС МТ, ни касса, ни SQL здесь не имитируются как проверенный запуск.
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const { QUEUE_KEY } = require('../erp_basic/public/marking-app.js');
const { RECOVERY_KEY } = require('../erp_basic/public/server-api.js');
const permissions = { create: true, scan: true, complete: true, configure: false, import: false };
const readiness = { ready: false, mode: 'Test', blockers: [{ code: 'GROUP_UNAPPROVED', message: 'Товарная группа не утверждена.' }, { code: 'ADAPTER_MISSING', message: 'Касса и подпись не подключены.' }] };
const profiles = [{ id: 'test-pu-v1', label: 'Проверка по экземплярам', environment: 'Test', mode: 'unit' }, { id: 'test-osu-v1', label: 'Учебный ОСУ', environment: 'Test', mode: 'quantity' }];
const session = { authenticated: true, business_date: '2026-11-08', user: 'marking@example.test', full_name: 'Оператор маркировки', csrf_token: 'csrf-fixture', companies: [{ name: 'Company A' }, { name: 'Company B' }], roles: ['ERP Basic Marking User'], marking: { read: true, write: true }, capabilities: {} };
const baseJob = { name: 'MARK-0001', company: 'Company A', role: 'manufacturer', profile: 'test-pu-v1', operation: 'manufacture', status: 'Draft', revision: 1, expected_quantity: 2, accepted_count: 0, rejected_count: 0, environment: 'Test', lines: [{ item_code: 'SKU-01', gtin: '00012345678905', quantity: 2, accepted_quantity: 0 }] };
const raw = ']d2010001234567890521Case001\x1d91TEST\x1d92proof';
const jobDetail = (job, overrides = {}) => ({ job, scans: [], history: [{ action: 'Created', message: 'Задание создано', actor: session.user, creation: '2026-11-08 12:00:00' }], start: 0, page_length: 25, has_more: false, permissions, readiness, ...overrides });
const workspace = (params, overrides = {}) => ({ company: params.company, roles: ['manufacturer', 'wholesale', 'retail'], profiles, permissions, readiness, jobs: params.role === 'manufacturer' ? [baseJob] : [], start: Number(params.start || 0), page_length: 25, has_more: false, ...overrides });
const query = route => Object.fromEntries(new URL(route.request().url()).searchParams);
async function respond(route, value, status = 200) { await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ message: value }) }); }
let browser, server, origin;
async function fixture(handle = async () => false) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await page.route('**/api/method/**', async route => {
    const method = new URL(route.request().url()).pathname.split('.').at(-1).split('/').at(-1);
    if (await handle(route, method)) return;
    if (method === 'session') return respond(route, session);
    if (method === 'workspace') return respond(route, workspace(query(route)));
    if (method === 'get_job') return respond(route, jobDetail({ ...baseJob, company: query(route).company }));
    await route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ exc_type: 'DoesNotExistError' }) });
  });
  return { page, errors };
}
async function openJob(page) {
  await page.goto(`${origin}/erp?view=marking`);
  await page.locator('[data-marking-action="job"]').click();
  await page.locator('#marking-scan').waitFor();
}
async function capture(page, filename) {
  if (!process.env.ERP_BASIC_SCREENSHOTS) return;
  await fs.mkdir(process.env.ERP_BASIC_SCREENSHOTS, { recursive: true });
  await page.screenshot({ path: path.join(process.env.ERP_BASIC_SCREENSHOTS, filename), fullPage: true, animations: 'disabled' });
}
test.before(async () => {
  server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const filename = url.pathname === '/erp' ? path.join(root, 'erp_basic/www/erp.html') : url.pathname.startsWith('/assets/erp_basic/') ? path.join(root, 'erp_basic/public', path.basename(url.pathname)) : '';
    if (!filename) { res.writeHead(404); res.end(); return; }
    try { const content = await fs.readFile(filename); res.setHeader('Content-Type', filename.endsWith('.js') ? 'application/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html'); res.end(content); }
    catch (_) { res.writeHead(404); res.end(); }
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined, args: ['--no-sandbox'] });
});
test.after(async () => { if (browser) await browser.close(); if (server?.listening) await new Promise(resolve => server.close(resolve)); });

test('ЧЗ UI: три рабочих места, учебный статус и внешние блокировки', async () => {
  const { page, errors } = await fixture();
  try {
    await openJob(page);
    assert.equal(await page.getByRole('button', { name: 'Заказать коды в СУЗ', exact: true }).isDisabled(), true);
    await page.getByText('Учебные данные · TEST', { exact: true }).waitFor();
    await page.getByText('Не связано с учётным документом', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Завершить локальную подготовку', exact: true }).isDisabled(), true);
    await page.getByRole('button', { name: 'Приёмка и отгрузка', exact: true }).click();
    assert.equal(await page.getByRole('button', { name: 'Передать документ через ЭДО', exact: true }).isDisabled(), true);
    await page.getByRole('button', { name: 'Продажа и возврат', exact: true }).click();
    assert.equal(await page.getByRole('button', { name: 'Оплатить и фискализировать', exact: true }).isDisabled(), true);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('ЧЗ UI: непрерывный скан не теряет следующий ввод и сериализует версии', async () => {
  const writes = []; let release;
  const gate = new Promise(resolve => { release = resolve; });
  const { page, errors } = await fixture(async (route, method) => {
    if (method !== 'scan_code') return false;
    const body = route.request().postDataJSON(); writes.push(body);
    if (writes.length === 1) await gate;
    const count = writes.length;
    await respond(route, { job: { ...baseJob, revision: 1 + count, accepted_count: count, lines: [{ ...baseJob.lines[0], accepted_quantity: count }] }, scan: { name: `SCAN-${count}`, result: 'Accepted', code_masked: '000123…001', reason: 'Локально принят. Внешняя проверка не выполнялась.', creation: '2026-11-08 12:01:00' } });
    return true;
  });
  try {
    await openJob(page);
    const input = page.locator('#marking-scan');
    await input.fill(raw); await input.press('Enter');
    await page.waitForFunction(() => Boolean(sessionStorage.getItem('erp-basic:pending-command:v1')));
    await input.fill('next-partial');
    assert.equal(await page.locator('#server-company').isDisabled(), true);
    release();
    await page.locator('.marking-result strong').filter({ hasText: 'Принят сервером' }).waitFor();
    assert.equal(await input.inputValue(), 'next-partial');
    assert.equal(await input.evaluate(node => document.activeElement === node), true);
    await input.fill(raw.replace('Case001', 'Case002')); await input.press('Enter');
    await page.waitForFunction(() => document.querySelectorAll('.marking-scans tbody tr').length === 2);
    assert.equal(writes[0].raw_code, raw); assert.equal(writes[0].expected_revision, 1); assert.equal(writes[1].expected_revision, 2);
    assert.equal(writes[0].sequence, 1); assert.equal(writes[1].sequence, 2);
    assert.equal(writes[0].device_id, writes[1].device_id); assert.notEqual(writes[0].event_id, writes[1].event_id);
    assert.equal(await page.evaluate(key => JSON.parse(sessionStorage.getItem(key)).queue, QUEUE_KEY), null);
    assert.deepEqual(errors, []);
  } finally { release(); await page.close(); }
});

test('ЧЗ UI: потеря ответа и reload сохраняют исходный запрос и очередь', async () => {
  const bodies = [];
  const { page, errors } = await fixture(async (route, method) => {
    if (method !== 'scan_code') return false;
    bodies.push(route.request().postData());
    if (bodies.length === 1) { await route.abort('connectionfailed'); return true; }
    await respond(route, { job: { ...baseJob, revision: 2, accepted_count: 1 }, scan: { name: 'SCAN-1', result: 'Accepted', code_masked: '000…001', reason: 'Сохранённый результат', creation: '2026-11-08' } }); return true;
  });
  try {
    await openJob(page); await page.locator('#marking-scan').fill(raw); await page.locator('#marking-scan').press('Enter');
    await page.getByText('Результат операции пока не подтверждён.', { exact: true }).waitFor();
    assert.equal(await page.locator('#marking-scan').isDisabled(), true);
    await page.reload();
    await page.getByRole('button', { name: 'Повторить тот же запрос', exact: true }).waitFor();
    assert.equal(bodies.length, 1, 'Reload не отправляет неизвестный результат автоматически');
    await page.getByRole('button', { name: 'Повторить тот же запрос', exact: true }).click();
    await page.getByText('Сохранённый результат', { exact: true }).first().waitFor();
    assert.equal(bodies[0], bodies[1]);
    assert.equal(await page.evaluate(key => sessionStorage.getItem(key), RECOVERY_KEY), null);
    assert.equal(await page.evaluate(key => JSON.parse(sessionStorage.getItem(key)).queue, QUEUE_KEY), null);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('ЧЗ UI: конфликт версии сохраняет event_id до явного продолжения', async () => {
  let revision = 1; const writes = [];
  const { page, errors } = await fixture(async (route, method) => {
    if (method === 'get_job') { await respond(route, jobDetail({ ...baseJob, revision })); return true; }
    if (method !== 'scan_code') return false;
    const body = route.request().postDataJSON(); writes.push(body);
    if (writes.length === 1) { revision = 7; await route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ exc_type: 'ConflictError' }) }); return true; }
    await respond(route, { job: { ...baseJob, revision: 8, accepted_count: 1 }, scan: { name: 'SCAN-1', result: 'Accepted', code_masked: '000…001', reason: 'После обновления версии', creation: '2026-11-08' } }); return true;
  });
  try {
    await openJob(page); await page.locator('#marking-scan').fill(raw); await page.locator('#marking-scan').press('Enter');
    await page.getByRole('button', { name: 'Загрузить версию и продолжить очередь', exact: true }).click();
    await page.getByText('После обновления версии', { exact: true }).first().waitFor();
    assert.equal(writes[0].expected_revision, 1); assert.equal(writes[1].expected_revision, 7);
    assert.equal(writes[0].event_id, writes[1].event_id); assert.equal(writes[0].sequence, writes[1].sequence);
    assert.notEqual(writes[0].idempotency_key, writes[1].idempotency_key);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('ЧЗ UI: несколько SKU и ОСУ используют фактическое количество без скана КМ', async () => {
  const writes = [];
  const { page, errors } = await fixture(async (route, method) => {
    if (!['create_job', 'record_quantity', 'complete_job'].includes(method)) return false;
    const body = route.request().postDataJSON(); writes.push({ method, body });
    const job = { ...baseJob, profile: 'test-osu-v1', expected_quantity: 3, lines: [{ item_code: 'SKU-01', gtin: '00012345678905', quantity: 2, accepted_quantity: 0 }, { item_code: 'SKU-02', gtin: '00012345678912', quantity: 1, accepted_quantity: 0 }] };
    if (method === 'record_quantity') { job.revision = 2; job.accepted_count = 2; job.lines[0].accepted_quantity = 2; }
    await respond(route, jobDetail(job)); return true;
  });
  try {
    await page.goto(`${origin}/erp?view=marking`);
    await page.getByRole('button', { name: 'Новое', exact: true }).click();
    await page.locator('#marking-profile').selectOption('test-osu-v1');
    await page.locator('#marking-item-0').fill('SKU-01'); await page.locator('#marking-gtin-0').fill('00012345678905'); await page.locator('#marking-quantity-0').fill('2');
    await page.getByRole('button', { name: '+ Строка', exact: true }).click();
    await page.locator('#marking-item-1').fill('SKU-02'); await page.locator('#marking-gtin-1').fill('00012345678912');
    await page.getByRole('button', { name: 'Сохранить задание', exact: true }).click();
    await page.getByRole('heading', { name: 'Фактическое количество · ОСУ', exact: true }).waitFor();
    assert.equal(await page.locator('#marking-scan').count(), 0);
    assert.equal(writes[0].body.lines.length, 2);
    assert.equal(writes[0].body.lines[0].gtin, '00012345678905');
    await page.locator('#marking-actual-0').fill('2'); await page.locator('.marking-quantity-form').first().getByRole('button').click();
    await page.getByText('Фактическое количество сохранено на сервере. Регуляторный формат определяется профилем строки.', { exact: true }).waitFor();
    assert.equal(writes[1].method, 'record_quantity'); assert.equal(writes[1].body.quantity, 2); assert.equal(writes[1].body.expected_revision, 1);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('ЧЗ UI: серверный текст экранируется, журнал и список имеют страницы', async () => {
  const reads = []; const malicious = '<img src=x onerror="window.injected=1">';
  const { page, errors } = await fixture(async (route, method) => {
    const params = query(route);
    if (method === 'workspace') { reads.push(params); await respond(route, workspace(params, { jobs: [{ ...baseJob, name: params.start === '25' ? 'MARK-0026' : baseJob.name }], has_more: params.start === '0' })); return true; }
    if (method === 'get_job') { reads.push(params); await respond(route, jobDetail(baseJob, { start: Number(params.start), has_more: params.start === '0', scans: [{ name: 'S-1', result: 'Rejected', code_masked: malicious, reason: malicious, creation: '2026-11-08' }] })); return true; }
    return false;
  });
  try {
    await openJob(page);
    assert.equal(await page.locator('.marking-scans img').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    assert.ok((await page.locator('.marking-scans').textContent()).includes(malicious));
    await page.getByRole('button', { name: 'Следующие сканы', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.marking-scans .marking-pagination').textContent.includes('26–26'));
    await page.locator('.marking-jobs').getByRole('button', { name: 'Далее', exact: true }).click();
    await page.getByText('MARK-0026', { exact: true }).waitFor();
    assert.ok(reads.some(row => row.start === '25' && row.page_length === '25' && row.name === baseJob.name));
    assert.ok(reads.some(row => row.start === '25' && row.role === 'manufacturer'));
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('ЧЗ UI: ожидаемый состав сохраняется отдельно от сканов, расхождения имеют страницы', async () => {
  let imported;
  const initial = { enabled: false, missing_count: 0, extra_count: 0, missing: [] };
  const { page, errors } = await fixture(async (route, method) => {
    if (method === 'get_job') { await respond(route, jobDetail(baseJob, { permissions: { ...permissions, import: true }, discrepancy: initial })); return true; }
    if (method === 'import_expected_codes') {
      imported = route.request().postDataJSON();
      await respond(route, jobDetail({ ...baseJob, revision: 2 }, { permissions: { ...permissions, import: true }, discrepancy: { enabled: true, missing_count: 26, extra_count: 0, missing: [{ code_masked: '000…001', gtin: baseJob.lines[0].gtin }], start: 0, page_length: 25, has_more: true } })); return true;
    }
    if (method === 'get_discrepancy') {
      assert.equal(query(route).start, '25');
      await respond(route, { enabled: true, missing_count: 26, extra_count: 0, missing: [{ code_masked: '000…026', gtin: baseJob.lines[0].gtin }], start: 25, page_length: 25, has_more: false }); return true;
    }
    return false;
  });
  try {
    await openJob(page); await page.locator('.marking-import summary').click();
    const codes = ['010001234567890521Case001', '010001234567890521Case002'];
    await page.locator('#marking-expected-codes').fill(codes.join('\n'));
    assert.equal(await page.locator('#marking-scan').isDisabled(), true);
    await page.getByRole('button', { name: 'Сохранить ожидаемый состав', exact: true }).click();
    await page.getByText('Ожидаемый состав сохранён отдельно от фактических сканов. Сверка покажет недостающие и лишние экземпляры.', { exact: true }).waitFor();
    assert.deepEqual(imported.codes, codes); assert.equal(imported.expected_revision, 1);
    assert.equal(await page.locator('#marking-expected-codes').inputValue(), '');
    assert.equal(await page.locator('#marking-scan').isDisabled(), false);
    await page.getByRole('button', { name: 'Следующие расхождения', exact: true }).click();
    await page.getByText('000…026', { exact: true }).waitFor();
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test('ЧЗ UI: поздний ответ Company A не появляется после перехода в B', async () => {
  let release; const gate = new Promise(resolve => { release = resolve; });
  const { page, errors } = await fixture(async (route, method) => {
    if (method !== 'workspace') return false;
    const params = query(route);
    if (params.company === 'Company A') await gate;
    await respond(route, workspace(params, { jobs: [{ ...baseJob, name: params.company + ' job', company: params.company }] })); return true;
  });
  try {
    await page.goto(`${origin}/erp?view=marking`);
    await page.locator('#server-company').selectOption('Company B');
    await page.getByText('Company B job', { exact: true }).waitFor();
    release();
    await page.waitForTimeout(100);
    assert.equal(await page.getByText('Company A job', { exact: true }).count(), 0);
    assert.deepEqual(errors, []);
  } finally { release(); await page.close(); }
});

test('ЧЗ UI: роль чтения, мобильный экран, тема и отсутствие demo fallback', async () => {
  const { page, errors } = await fixture(async (route, method) => {
    if (method === 'get_job') { await respond(route, jobDetail({ ...baseJob, role: 'retail', operation: 'sale' }, { permissions: { create: false, scan: false, complete: false } })); return true; }
    return false;
  });
  try {
    await openJob(page);
    assert.equal(await page.locator('#marking-scan').isDisabled(), true);
    assert.equal(await page.getByRole('button', { name: 'Завершить локальную подготовку', exact: true }).count(), 0);
    await capture(page, 'marking-desktop.png');
    await page.getByRole('button', { name: 'Сменить тему', exact: true }).click();
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    await capture(page, 'marking-dark.png');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().right <= document.querySelector('.rail').getBoundingClientRect().right + 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    await capture(page, 'marking-mobile.png');
    assert.equal(await page.locator('script[src="/assets/app.js"]').count(), 0);
    assert.deepEqual(await page.evaluate(() => Object.keys(localStorage)), ['erpbasic-theme']);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});
