'use strict';
// Настоящий путь UI → HTTP → Frappe/MariaDB. Подмены маршрутов, ответов и кассовых квитанций здесь нет.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');
const baseURL = process.env.ERP_BASIC_LIVE_URL;
const credentialsPath = process.env.ERP_BASIC_LIVE_CREDENTIALS;
const resultPath = process.env.ERP_BASIC_LIVE_RESULT;
let browser, credentials;

test.before(async () => {
  assert.ok(baseURL && credentialsPath && resultPath, 'Нужны URL отдельного Site, файл credentials и путь результата');
  const url = new URL(baseURL);
  assert.ok(url.protocol === 'http:' && (url.hostname === 'localhost' || url.hostname.endsWith('.localhost')), 'Тест разрешён только на отдельном локальном Site');
  assert.equal((await fs.stat(credentialsPath)).mode & 0o077, 0, 'Credentials доступны только владельцу');
  credentials = JSON.parse(await fs.readFile(credentialsPath, 'utf8'));
  assert.equal(credentials.version, 1); assert.equal(credentials.site, url.hostname);
  assert.ok(credentials.marking && credentials.marking.raw_codes?.length === 2 && credentials.marking.ki_codes?.length === 2, 'Тестовые коды подготавливает marking_browser.prepare');
  assert.equal(credentials.marking.profile, 'test-pu-v1');
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined, args: ['--no-sandbox'] });
});
test.after(async () => { if (browser) await browser.close(); });

function waitCommand(page, method) {
  return page.waitForResponse(response => new URL(response.url()).pathname === `/api/method/erp_basic.marking.api.${method}` && response.request().method() === 'POST');
}
async function message(response) {
  const body = await response.json();
  assert.equal(response.status(), 200, `Реальный HTTP API отклонил операцию: ${body.exc_type || response.status()}`);
  assert.ok(body.message && !body.exc_type && !body.exception, 'Результат должен подтвердить сервер');
  return body.message;
}
async function capture(page, filename) {
  if (!process.env.ERP_BASIC_SCREENSHOTS) return;
  await fs.mkdir(process.env.ERP_BASIC_SCREENSHOTS, { recursive: true });
  await page.screenshot({ path: path.join(process.env.ERP_BASIC_SCREENSHOTS, filename), fullPage: true, animations: 'disabled', mask: [page.locator('input[type=password]'), page.locator('#marking-scan'), page.locator('#marking-expected-codes')] });
}
async function scan(page, rawCode, expected) {
  const response = waitCommand(page, 'scan_code');
  await page.locator('#marking-scan').fill(rawCode); await page.locator('#marking-scan').press('Enter');
  const result = await message(await response);
  assert.equal(result.scan.result, expected);
  await page.waitForFunction(() => {
    const pending = sessionStorage.getItem('erp-basic:pending-command:v1');
    const queue = JSON.parse(sessionStorage.getItem('erp-basic:marking-scans:v1') || '{}').queue;
    return !pending && !queue;
  });
  assert.equal(await page.locator('#marking-scan').inputValue(), '');
  assert.equal(await page.locator('#marking-scan').evaluate(node => document.activeElement === node), true);
  return result;
}

test('Живая маркировка: ожидаемый состав, два скана, дубль, локальная подготовка и повторное чтение', { timeout: 150000 }, async () => {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1050 }, locale: 'ru-RU' });
  const page = await context.newPage(); page.setDefaultTimeout(25000);
  const errors = []; page.on('pageerror', error => errors.push(error.message)); page.on('dialog', dialog => dialog.accept());
  const data = credentials.marking;
  try {
    const url = new URL('/erp', baseURL); url.searchParams.set('view', 'marking'); url.searchParams.set('company', data.company);
    await page.goto(url.href);
    await page.locator('#server-username').fill(credentials.user); await page.locator('#server-password').fill(credentials.password);
    await page.getByRole('button', { name: 'Войти', exact: true }).click();
    await page.getByRole('heading', { name: 'Маркировка', exact: true }).waitFor();
    assert.equal(await page.locator('#server-company').inputValue(), data.company);
    await page.getByRole('button', { name: 'Приёмка и отгрузка', exact: true }).click();
    await page.getByRole('button', { name: 'Новое', exact: true }).click();
    await page.locator('#marking-operation').selectOption(data.operation);
    await page.locator('#marking-profile').selectOption(data.profile);
    await page.locator('#marking-item-0').fill(data.item_code); await page.locator('#marking-gtin-0').fill(data.gtin); await page.locator('#marking-quantity-0').fill('2');
    const createResponse = waitCommand(page, 'create_job');
    await page.getByRole('button', { name: 'Сохранить задание', exact: true }).click();
    const created = await message(await createResponse); const name = created.job.name;
    assert.equal(created.job.company, data.company); assert.equal(created.job.role, data.role); assert.equal(created.job.status, 'Draft');
    assert.equal(created.job.environment, 'Test'); assert.equal(created.job.accepted_count, 0);
    await page.getByText('Учебные данные · TEST', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Передать документ через ЭДО', exact: true }).isDisabled(), true);
    await page.locator('.marking-import summary').click();
    await page.locator('#marking-expected-codes').fill(data.ki_codes.join('\n'));
    const importResponse = waitCommand(page, 'import_expected_codes');
    await page.getByRole('button', { name: 'Сохранить ожидаемый состав', exact: true }).click();
    const imported = await message(await importResponse);
    assert.equal(imported.discrepancy.missing_count, 2); assert.equal(imported.discrepancy.extra_count, 0);
    await page.locator('#marking-scan').waitFor({ state: 'visible' });
    await page.waitForFunction(() => !document.querySelector('#marking-scan').disabled);
    await scan(page, data.raw_codes[0], 'Accepted');
    const second = await scan(page, data.raw_codes[1], 'Accepted'); assert.equal(second.job.accepted_count, 2);
    const duplicate = await scan(page, data.raw_codes[0], 'Duplicate'); assert.equal(duplicate.job.accepted_count, 2); assert.ok(duplicate.job.rejected_count >= 1);
    await page.getByRole('button', { name: 'Обновить сверку', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.marking-discrepancy-counts')?.textContent.includes('Недостающих: 0'));
    const completeResponse = waitCommand(page, 'complete_job');
    await page.getByRole('button', { name: 'Завершить локальную подготовку', exact: true }).click();
    const completed = await message(await completeResponse);
    assert.equal(completed.job.status, 'Prepared'); assert.equal(completed.job.native_posted, 0);
    assert.equal(completed.job.external_state, 'Unverified'); assert.equal(completed.job.accepted_count, 2);
    await page.locator('#marking-job-status').filter({ hasText: 'Подготовлено локально' }).waitFor();
    assert.equal(await page.locator('#marking-scan').count(), 0);
    await capture(page, 'marking-live-light.png');
    await page.reload();
    await page.getByRole('button', { name: 'Приёмка и отгрузка', exact: true }).click();
    await page.locator('[data-marking-action="job"]').filter({ has: page.getByText(name, { exact: true }) }).click();
    await page.locator('#marking-job-status').filter({ hasText: 'Подготовлено локально' }).waitFor();
    await page.getByRole('button', { name: 'Сменить тему', exact: true }).click();
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    await capture(page, 'marking-live-dark.png');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().right <= document.querySelector('.rail').getBoundingClientRect().right + 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, 'Таблицы прокручиваются внутри рабочего места');
    await capture(page, 'marking-live-mobile.png');
    assert.equal(await page.evaluate(() => sessionStorage.getItem('erp-basic:pending-command:v1')), null);
    assert.deepEqual(await page.evaluate(() => Object.keys(localStorage)), ['erpbasic-theme']);
    assert.deepEqual(errors, []);
    await fs.mkdir(path.dirname(resultPath), { recursive: true });
    await fs.writeFile(resultPath, JSON.stringify({ site: credentials.site, name, mocks: false, accepted_count: 2, status: 'Prepared', external_transmission: false }, null, 2), { mode: 0o600 });
  } finally { await context.close(); }
});
