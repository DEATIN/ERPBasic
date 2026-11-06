'use strict';
// Настоящий сценарий UI → HTTP → ERPNext. Здесь нет подмены маршрутов или ответов API.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');

const baseURL = process.env.ERP_BASIC_LIVE_URL;
const credentialsPath = process.env.ERP_BASIC_LIVE_CREDENTIALS;
const resultPath = process.env.ERP_BASIC_LIVE_RESULT;
const screenshots = process.env.ERP_BASIC_SCREENSHOTS;
let browser, credentials;

test.before(async () => {
  assert.ok(baseURL && credentialsPath && resultPath, 'Живой браузерный тест требует URL, файл учётной записи и путь результата');
  const url = new URL(baseURL);
  assert.ok(url.protocol === 'http:' && (url.hostname === 'localhost' || url.hostname.endsWith('.localhost')), 'Разрешён только отдельный локальный тестовый Site');
  const metadata = await fs.stat(credentialsPath);
  assert.equal(metadata.mode & 0o077, 0, 'Файл случайного пароля не должен быть доступен другим пользователям');
  credentials = JSON.parse(await fs.readFile(credentialsPath, 'utf8'));
  assert.equal(credentials.version, 1);
  assert.equal(url.hostname, credentials.site);
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined, args: ['--no-sandbox'] });
});
test.after(async () => { if (browser) await browser.close(); });

async function capture(page, name) {
  if (!screenshots) return;
  await fs.mkdir(screenshots, { recursive: true });
  await page.screenshot({ path: path.join(screenshots, name), fullPage: true, animations: 'disabled', mask: [page.locator('input[type="password"]')] });
}
async function message(response) {
  const body = await response.json();
  let validation = '';
  try {
    validation = JSON.parse(body._server_messages || '[]').map(entry => {
      try {
        const message = (typeof entry === 'string' ? JSON.parse(entry) : entry)?.message;
        return typeof message === 'string' ? message : '';
      } catch (_) { return ''; }
    }).join(' ').slice(0, 800);
  } catch (_) { /* Неверный формат ошибки не должен скрывать HTTP-статус. */ }
  assert.equal(response.status(), 200, `Настоящий ERP API должен подтвердить операцию: ${body.exc_type || ''} ${validation}`);
  assert.ok(body.message && !body.exc_type, 'ERP API должен вернуть успешный результат');
  return body.message;
}
async function login(page) {
  await page.locator('#server-username').fill(credentials.user);
  await page.locator('#server-password').fill(credentials.password);
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await page.getByRole('button', { name: 'Выйти', exact: true }).waitFor();
  assert.equal(await page.locator('#server-company').inputValue(), credentials.company);
}
async function posted(page, name) {
  await page.getByText('Проведён', { exact: true }).waitFor();
  assert.equal(await page.locator('.top-title strong').textContent(), name);
  assert.equal(Number(await page.locator('#field-items-0-qty').inputValue()), 2);
  assert.equal(await page.locator('#field-items-0-item_code').inputValue(), credentials.item);
  assert.equal(await page.locator('#field-items-0-qty').isDisabled(), true);
  const total = await page.locator('.server-summary > div').filter({ has: page.getByText('Итого', { exact: true }) }).locator('dd').textContent();
  assert.match(total, /251,00/, 'Сумма в интерфейсе должна совпадать с подтверждённым серверным расчётом');
}

test('Живой ERP: сотрудник создаёт и проводит заказ через UI, после перезагрузки и нового входа видит тот же документ', { timeout: 120000 }, async () => {
  const context = await browser.newContext({ viewport: { width: 1365, height: 900 }, locale: 'ru-RU' });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  try {
    const start = new URL('/erp', baseURL);
    start.searchParams.set('kind', 'orders'); start.searchParams.set('company', credentials.company);
    await page.goto(start.href);
    await login(page);
    await page.getByRole('button', { name: 'Создать', exact: true }).click();
    assert.equal(await page.locator('#field-transaction_date').inputValue(), credentials.business_date);
    await page.locator('#field-customer').fill(credentials.customer);
    await page.locator('#field-delivery_date').fill(credentials.business_date);
    await page.locator('#field-selling_price_list').fill(credentials.price_list);
    await page.locator('#field-items-0-item_code').fill(credentials.item);
    await page.locator('#field-items-0-qty').fill('2');
    await page.locator('#field-items-0-rate').fill('125.50');
    const saveResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/method/erp_basic.api.save_document' && response.request().method() === 'POST');
    await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
    const saved = await message(await saveResponse);
    const name = saved.document.name;
    assert.ok(name && name !== credentials.item);
    assert.equal(saved.document.docstatus, 0);
    assert.equal(saved.document.grand_total, credentials.expected_total);
    await page.getByRole('button', { name: 'Провести', exact: true }).waitFor();
    assert.equal(await page.locator('.top-title strong').textContent(), name);
    const submitResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/method/erp_basic.api.submit_document' && response.request().method() === 'POST');
    await page.getByRole('button', { name: 'Провести', exact: true }).click();
    const submitted = await message(await submitResponse);
    assert.equal(submitted.document.name, name); assert.equal(submitted.document.docstatus, 1);
    assert.equal(submitted.document.grand_total, credentials.expected_total);
    await posted(page, name);
    const documentURL = page.url();
    assert.equal(new URL(documentURL).searchParams.get('name'), name);
    await capture(page, 'live-order-light.png');
    await page.reload();
    await posted(page, name);
    await page.getByRole('button', { name: 'Сменить тему', exact: true }).click();
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    await capture(page, 'live-order-dark.png');
    await page.getByRole('button', { name: 'Выйти', exact: true }).click();
    await page.getByRole('button', { name: 'Войти', exact: true }).waitFor();
    await login(page);
    await page.goto(documentURL);
    await posted(page, name);
    const readURL = new URL('/api/method/erp_basic.api.get_document', baseURL);
    readURL.searchParams.set('kind', 'orders'); readURL.searchParams.set('company', credentials.company); readURL.searchParams.set('name', name);
    const persisted = await message(await context.request.get(readURL.href));
    assert.equal(persisted.document.name, name); assert.equal(persisted.document.docstatus, 1);
    assert.equal(persisted.document.grand_total, credentials.expected_total);
    assert.deepEqual(await page.evaluate(() => Object.keys(localStorage)), ['erpbasic-theme']);
    assert.equal(await page.evaluate(() => sessionStorage.getItem('erp-basic:pending-command:v1')), null);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().right <= document.querySelector('.rail').getBoundingClientRect().right + 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    await capture(page, 'live-order-mobile.png');
    assert.deepEqual(errors, []);
    await fs.mkdir(path.dirname(resultPath), { recursive: true });
    await fs.writeFile(resultPath, JSON.stringify({ site: credentials.site, company: credentials.company, name,
      kind: 'orders', docstatus: persisted.document.docstatus, grand_total: persisted.document.grand_total,
      browser: 'Chromium', transport: 'настоящий HTTP', mocks: false, reloaded: true, relogged: true }, null, 2));
  } catch (error) {
    await capture(page, 'live-order-failure.png');
    throw error;
  } finally { await context.close(); }
});
