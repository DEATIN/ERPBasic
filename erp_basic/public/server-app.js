/* Серверный интерфейс ERP Basic. Сохранённое состояние меняют только подтверждённые ответы Frappe. */
(function () {
  'use strict';

  const { Client, ApiError } = window.ERPBasicAPI;
  const api = new Client();
  const app = document.getElementById('server-app');
  const dialog = document.getElementById('server-dialog');
  const state = { session: null, company: '', view: 'home', kind: '', doc: null, serverDocument: null, permissions: {}, dirty: false,
    busy: false, loading: false, error: '', unknown: null, rows: [], start: 0, pageLength: 25, hasMore: false,
    search: '', requestId: 0, lookup: null, authIssue: false, requestedName: '', warehouse: '', recoveryIssue: false, exchange: null, exchangeDirection: 'incoming', exchangeNotice: '' };
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const clone = value => JSON.parse(JSON.stringify(value));
  const today = () => /^\d{4}-\d{2}-\d{2}$/.test(state.session?.business_date || '') ? state.session.business_date : '';
  const icon = name => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${({
    home: '<path d="m3 10 9-7 9 7v10H3Z"/><path d="M9 20v-7h6v7"/>',
    sales: '<path d="M4 19V5h16v14Z"/><path d="M8 9h8M8 13h5"/>',
    purchase: '<path d="M3 5h2l3 12h11l2-9H6"/><circle cx="9" cy="21" r="1"/><circle cx="18" cy="21" r="1"/>',
    stock: '<path d="m3 7 9-4 9 4v10l-9 4-9-4Z"/><path d="m3 7 9 4 9-4M12 11v10"/>',
    finance: '<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M3 10h18M7 15h3"/>',
    master: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-3a8 8 0 0 1 16 0v3"/>',
    settings: '<path d="M4 7h16M4 17h16"/><circle cx="8" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
    plus: '<path d="M12 5v14M5 12h14"/>', search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
    back: '<path d="m14 5-7 7 7 7"/>', refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 6a8 8 0 0 1 13 4M18 18a8 8 0 0 1-13-4"/>',
    theme: '<path d="M20 14A8 8 0 0 1 10 4a8 8 0 1 0 10 10Z"/>', close: '<path d="m6 6 12 12M18 6 6 18"/>',
    logout: '<path d="M10 4H4v16h6M9 12h12m-4-4 4 4-4 4"/>', menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  })[name] || '<circle cx="12" cy="12" r="7"/>'}</svg>`;
  const btn = (action, label, cls = 'outline', extra = '') => `<button type="button" class="btn ${cls}" data-action="${action}" ${extra}>${label}</button>`;
  const ref = (name, label, lookup, options = {}) => ({ name, label, type: 'reference', lookup, ...options });
  const date = (name, label, required = false) => ({ name, label, type: 'date', required });
  const text = (name, label, options = {}) => ({ name, label, type: 'text', ...options });
  const number = (name, label, options = {}) => ({ name, label, type: 'number', ...options });
  const select = (name, label, values, options = {}) => ({ name, label, type: 'select', values, ...options });
  const customer = ref('customer', 'Покупатель', 'customers', { required: true });
  const supplier = ref('supplier', 'Поставщик', 'suppliers', { required: true });
  const posting = date('posting_date', 'Дата', true);
  const transaction = date('transaction_date', 'Дата', true);
  const specs = {
    quotes: { title: 'Коммерческие предложения', singular: 'Коммерческое предложение', module: 'sales', items: 'trade', fields: [ref('party_name', 'Покупатель', 'customers', { required: true }), transaction, date('valid_till', 'Действительно до')], defaults: { quotation_to: 'Customer' }, maps: [['orders', 'Заказ покупателя']] },
    orders: { title: 'Заказы покупателей', singular: 'Заказ покупателя', module: 'sales', items: 'trade', fields: [customer, transaction, date('delivery_date', 'Дата поставки', true)], maps: [['deliveries', 'Отгрузка'], ['invoices', 'Счёт покупателю']] },
    deliveries: { title: 'Отгрузки', singular: 'Отгрузка', module: 'sales', items: 'trade', fields: [customer, posting], maps: [['invoices', 'Счёт покупателю']] },
    invoices: { title: 'Счета покупателям', singular: 'Счёт покупателю', module: 'sales', items: 'trade', fields: [customer, posting, date('due_date', 'Срок оплаты')], defaults: { update_stock: 0 }, maps: [['payments', 'Получение оплаты']] },
    purchaseOrders: { title: 'Заказы поставщикам', singular: 'Заказ поставщику', module: 'purchase', items: 'trade', fields: [supplier, transaction, date('schedule_date', 'Дата поставки', true)], maps: [['receipts', 'Приёмка'], ['purchaseInvoices', 'Счёт поставщика']] },
    receipts: { title: 'Приёмки', singular: 'Приёмка', module: 'purchase', items: 'trade', fields: [supplier, posting], maps: [['purchaseInvoices', 'Счёт поставщика']] },
    purchaseInvoices: { title: 'Счета поставщиков', singular: 'Счёт поставщика', module: 'purchase', items: 'trade', fields: [supplier, posting, date('due_date', 'Срок оплаты')], defaults: { update_stock: 0 }, maps: [['payments', 'Оплата поставщику']] },
    stockDocs: { title: 'Складские документы', singular: 'Складской документ', module: 'stock', items: 'stock', fields: [select('stock_entry_type', 'Операция', [['Material Receipt', 'Поступление'], ['Material Issue', 'Списание'], ['Material Transfer', 'Перемещение']], { required: true }), posting], defaults: { stock_entry_type: 'Material Receipt', purpose: 'Material Receipt' } },
    inventory: { title: 'Инвентаризации', singular: 'Инвентаризация', module: 'stock', items: 'inventory', fields: [posting], defaults: { purpose: 'Stock Reconciliation' } },
    payments: { title: 'Платежи', singular: 'Платёж', module: 'finance', fields: [select('payment_type', 'Тип платежа', [['Receive', 'Получение'], ['Pay', 'Оплата'], ['Internal Transfer', 'Перевод']], { required: true }), posting, select('party_type', 'Тип контрагента', [['Customer', 'Покупатель'], ['Supplier', 'Поставщик']]), ref('party', 'Контрагент', 'party'), ref('paid_from', 'Со счёта', 'accounts', { required: true }), ref('paid_to', 'На счёт', 'accounts', { required: true }), number('paid_amount', 'Сумма со счёта', { required: true, min: 0.01 }), number('received_amount', 'Сумма на счёт', { required: true, min: 0.01 }), text('reference_no', 'Номер банковской операции'), date('reference_date', 'Дата банковской операции')], defaults: { payment_type: 'Receive', party_type: 'Customer' } },
    items: { title: 'Номенклатура', singular: 'Товар или услуга', module: 'master', master: true, fields: [text('item_code', 'Код', { required: true }), text('item_name', 'Наименование', { required: true }), ref('item_group', 'Группа', 'itemGroups', { required: true }), ref('stock_uom', 'Единица измерения', 'uoms', { required: true }), { name: 'is_stock_item', label: 'Вести складской учёт', type: 'checkbox' }], defaults: { is_stock_item: 1 } },
    customers: { title: 'Покупатели', singular: 'Покупатель', module: 'master', master: true, fields: [text('customer_name', 'Наименование', { required: true }), select('customer_type', 'Тип', [['Company', 'Организация'], ['Individual', 'Физическое лицо']]), ref('customer_group', 'Группа', 'customerGroups'), ref('territory', 'Территория', 'territories')], defaults: { customer_type: 'Company' } },
    suppliers: { title: 'Поставщики', singular: 'Поставщик', module: 'master', master: true, fields: [text('supplier_name', 'Наименование', { required: true }), select('supplier_type', 'Тип', [['Company', 'Организация'], ['Individual', 'Физическое лицо']]), ref('supplier_group', 'Группа', 'supplierGroups')], defaults: { supplier_type: 'Company' } },
    warehouses: { title: 'Склады', singular: 'Склад', module: 'master', master: true, fields: [text('warehouse_name', 'Название', { required: true }), ref('parent_warehouse', 'Родительский склад', 'warehouses'), ref('account', 'Счёт учёта склада', 'accounts'), { name: 'is_group', label: 'Группа складов', type: 'checkbox' }] },
  };
  for (const kind of ['quotes', 'orders', 'deliveries', 'invoices']) specs[kind].fields.push(ref('selling_price_list', 'Прайс-лист', 'priceLists'));
  for (const kind of ['purchaseOrders', 'receipts', 'purchaseInvoices']) specs[kind].fields.push(ref('buying_price_list', 'Прайс-лист', 'priceLists'));
  for (const kind of ['quotes', 'orders', 'deliveries', 'invoices', 'purchaseOrders', 'receipts', 'purchaseInvoices']) {
    specs[kind].fields.push(number('additional_discount_percentage', 'Скидка, %', { min: 0, max: 100 }), select('apply_discount_on', 'База скидки', [['Grand Total', 'Итоговая сумма'], ['Net Total', 'Сумма без налогов']]));
  }
  const modules = { home: ['Главная', 'home'], sales: ['Продажи', 'sales'], purchase: ['Закупки', 'purchase'], stock: ['Склад', 'stock'], finance: ['Финансы', 'finance'], master: ['Справочники', 'master'], exchange: ['Обмен', 'refresh'] };
  const tradeChildFields = ['name', 'item_code', 'qty', 'rate', 'uom', 'conversion_factor', 'warehouse', 'delivery_date', 'schedule_date', 'sales_order', 'so_detail', 'against_sales_order', 'delivery_note', 'dn_detail', 'purchase_order', 'purchase_order_item', 'purchase_receipt', 'purchase_receipt_item', 'pr_detail', 'quotation_item', 'prevdoc_docname', 'expense_account', 'cost_center'];
  const stockChildFields = ['name', 'item_code', 'qty', 'uom', 'conversion_factor', 's_warehouse', 't_warehouse', 'basic_rate', 'expense_account', 'cost_center'];
  const inventoryChildFields = ['name', 'item_code', 'warehouse', 'qty', 'valuation_rate'];

  const additionalLookups = new Set(['accounts', 'uoms', 'itemGroups', 'customerGroups', 'supplierGroups', 'territories', 'priceLists', 'stockEntryTypes']);
  const commandLabels = { save_document: 'Сохранение документа', submit_document: 'Проведение документа', cancel_document: 'Отмена документа', create_related: 'Создание связанного документа', create_return: 'Создание возврата' };
  const commandResults = { save_document: 'Документ сохранён на сервере.', submit_document: 'Документ проведён на сервере.', cancel_document: 'Документ отменён на сервере.', create_related: 'Связанный черновик создан на сервере.', create_return: 'Черновик возврата создан на сервере.' };
  function canExchange() { return state.session?.roles?.some(role => ['System Manager', 'ERP Basic Integration'].includes(role)); }
  function capability(kind, action) { return state.session?.capabilities?.[kind]?.[action] === true; }
  function docPermission(action) { return state.permissions?.[action] === true || (!state.doc?.name && capability(state.kind, action)); }
  function currency(doc = state.doc) { return doc?.currency || state.session?.companies?.find(row => row.name === state.company)?.default_currency || ''; }
  function paymentCurrency(field, doc = state.serverDocument || state.doc) {
    if (field === 'paid_amount') return doc?.paid_from_account_currency || '';
    if (field === 'received_amount') return doc?.paid_to_account_currency || '';
    return doc?.party_account_currency || (doc?.payment_type === 'Receive' ? doc?.paid_from_account_currency : doc?.paid_to_account_currency) || '';
  }
  function amount(value, code = currency()) {
    if (value === undefined || value === null || value === '') return '—';
    try { return new Intl.NumberFormat('ru-RU', { ...(code ? { style: 'currency', currency: code } : {}), minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(value)); }
    catch (_) { return `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(Number(value))} ${esc(code)}`; }
  }
  function status(doc) {
    if (specs[state.kind]?.master) return '<span class="badge blue">Справочник</span>';
    const code = Number(doc.docstatus || 0);
    return `<span class="badge ${code === 1 ? 'green' : code === 2 ? 'red' : 'orange'}">${code === 1 ? 'Проведён' : code === 2 ? 'Отменён' : 'Черновик'}</span>`;
  }
  function setUrl() {
    const params = new URLSearchParams();
    if (state.company) params.set('company', state.company);
    if (state.kind) params.set('kind', state.kind);
    if (['stock', 'exchange'].includes(state.view)) params.set('view', state.view);
    if (state.view === 'document' && state.doc?.name) params.set('name', state.doc.name);
    window.history.replaceState(null, '', `/erp${params.size ? '?' + params.toString() : ''}`);
  }
  function toast(message) {
    const box = document.createElement('div'); box.className = 'toast'; box.textContent = message;
    document.getElementById('toasts').appendChild(box);
    window.setTimeout(() => box.remove(), 5500);
  }
  function showLogin(error = '', username = '') {
    state.session = null; state.company = ''; state.doc = null; state.rows = []; state.dirty = false; state.loading = false; state.recoveryIssue = false; state.unknown = null; state.authIssue = false;
    app.innerHTML = `<main id="content" class="server-login"><div class="server-brand"><span class="brand">B</span>${btn('theme', icon('theme'), 'icon-btn', 'aria-label="Сменить тему"')}</div><h1>ERP Basic</h1><p class="server-login-note">Вход в рабочую систему</p>${error ? `<div class="server-error" role="alert">${esc(error)}</div>` : ''}<form id="server-login-form"><div class="field"><label for="server-username">Пользователь</label><input id="server-username" name="username" value="${esc(username)}" autocomplete="username" required autofocus></div><div class="field"><label for="server-password">Пароль</label><input id="server-password" name="password" type="password" autocomplete="current-password" required></div><button class="btn primary" type="submit">Войти</button></form><p class="server-login-note">Используйте учётную запись этого сервера. Организации и доступные действия определяются вашими правами.</p></main>`;
    document.getElementById('server-username')?.focus();
  }
  function shell() {
    const module = ['stock', 'exchange'].includes(state.view) ? state.view : specs[state.kind]?.module || 'home';
    const readable = Object.entries(specs).filter(([kind]) => capability(kind, 'read'));
    const rail = Object.entries(modules).filter(([name]) => name === 'home' || (name === 'exchange' && canExchange()) || readable.some(([, spec]) => spec.module === name));
    const title = state.view === 'document' ? (state.doc?.name || `Новый: ${specs[state.kind].singular.toLowerCase()}`) : (state.view === 'stock' ? 'Остатки и стоимость' : state.view === 'exchange' ? 'Журнал обмена' : state.view === 'recovery' ? 'Незавершённая операция' : specs[state.kind]?.title || 'Рабочее место');
    app.innerHTML = `<div class="app-shell"><nav class="rail" aria-label="Разделы"><a class="brand" href="/erp" data-nav="home" aria-label="ERP Basic: главная">B</a>${rail.map(([name, [label, glyph]]) => `<a class="rail-link ${module === name ? 'active' : ''}" href="/erp" data-module="${name}" title="${label}" aria-label="${label}">${icon(glyph)}</a>`).join('')}<div class="rail-bottom">${btn('theme', icon('theme'), 'icon-btn', 'aria-label="Сменить тему"')}<span class="avatar" title="${esc(state.session.full_name || state.session.user)}">${esc((state.session.full_name || state.session.user || '?').slice(0, 1))}</span></div></nav><aside id="server-sidebar" class="sidebar"><div class="side-title">${modules[module][0]}</div><div class="side-tools"><label for="server-company" class="server-field-hint">Организация</label><select id="server-company" class="server-company" aria-label="Организация" ${state.busy || state.unknown || state.recoveryIssue ? 'disabled' : ''}>${state.session.companies.map(row => `<option value="${esc(row.name)}" ${row.name === state.company ? 'selected' : ''}>${esc(row.name)}</option>`).join('')}</select></div><nav class="side-nav" aria-label="Документы">${readable.filter(([, spec]) => module === 'home' || spec.module === module).map(([kind, spec]) => `<a class="side-item ${state.kind === kind ? 'active' : ''}" href="/erp?kind=${encodeURIComponent(kind)}" data-kind="${kind}">${spec.title}</a>`).join('')}${(module === 'stock' || module === 'home') && (capability('stockDocs', 'read') || capability('inventory', 'read')) ? `<a class="side-item ${state.view === 'stock' ? 'active' : ''}" href="/erp?view=stock" data-action="stock-report">Остатки и стоимость</a>` : ''}${module === 'exchange' ? '<a class="side-item active" href="/erp?view=exchange" data-action="exchange-journal">Журнал обмена</a>' : ''}</nav><div class="side-footer"><div class="server-connected">Серверный режим</div><div class="server-role">${esc(state.session.full_name || state.session.user)}</div>${btn('logout', icon('logout') + ' Выйти', 'outline', state.busy ? 'disabled' : '')}</div></aside><button type="button" class="mobile-overlay" data-action="close-menu" aria-label="Закрыть разделы" tabindex="-1"></button><main class="workspace" id="content"><header class="topbar">${btn('menu', icon('menu'), 'icon-btn mobile-toggle', 'aria-label="Открыть разделы" aria-controls="server-sidebar" aria-expanded="false"')}<div class="top-title"><strong>${esc(title)}</strong><span class="server-top-company">${esc(state.company)}</span></div><div class="actions" id="server-actions">${actions()}</div></header><div id="server-content"></div></main></div>`;
    renderContent();
  }
  function actions() {
    if (state.loading || state.recoveryIssue) return '';
    if (state.view === 'exchange') return btn('refresh-exchange', icon('refresh'), 'icon-btn', 'aria-label="Обновить журнал обмена"');
    if (state.view === 'stock') return btn('refresh-stock', icon('refresh'), 'icon-btn', 'aria-label="Обновить остатки"');
    if (state.view === 'list') return `${btn('refresh', icon('refresh'), 'icon-btn', 'aria-label="Обновить список"')}${capability(state.kind, 'create') ? btn('new', icon('plus') + ' Создать', 'primary') : ''}`;
    if (state.view !== 'document' || !state.doc) return '';
    const isMaster = specs[state.kind].master;
    const persisted = Boolean(state.doc.name);
    const docstatus = Number(state.doc.docstatus || 0);
    const disabled = state.busy || state.unknown ? 'disabled' : '';
    return `${btn('back', icon('back') + ' Список', 'outline', disabled)}${docstatus === 0 && docPermission(persisted ? 'write' : 'create') ? btn('save', state.busy ? 'Сохраняем…' : 'Сохранить', 'primary', disabled) : ''}${!isMaster && persisted && docstatus === 0 && docPermission('submit') ? btn('submit', 'Провести', 'primary', disabled || (state.dirty ? 'disabled title="Сначала сохраните изменения"' : '')) : ''}${!isMaster && persisted && docstatus === 1 && docPermission('cancel') ? btn('cancel', 'Отменить', 'outline', disabled) : ''}${persisted ? btn('reload-document', icon('refresh'), 'icon-btn', `${disabled} aria-label="Загрузить актуальную версию"`) : ''}`;
  }
  function messages() {
    if (state.unknown) return `<div class="server-notice server-unknown" role="alert"><p><strong>Результат операции пока не подтверждён.</strong> ${esc(state.error)}</p><p>Повтор использует тот же ключ и те же данные. Служебная запись запроса сохранена в этой вкладке и восстановится после перезагрузки.</p>${btn('retry-command', 'Повторить тот же запрос', 'outline', state.busy ? 'disabled' : '')}${state.authIssue ? btn('reauth', 'Войти повторно') : ''}</div>`;
    if (state.recoveryIssue) return `<div class="server-error" role="alert"><p>${esc(state.error)}</p><p>Новые записи заблокированы. После выхода найдите документ на сервере перед повторным вводом.</p>${btn('logout', 'Выйти из системы')}</div>`;
    return state.error ? `<div class="server-error" role="alert"><p>${esc(state.error)}</p>${state.authIssue ? btn('reauth', 'Войти повторно') : ''}${state.view !== 'document' ? btn('refresh', 'Повторить загрузку') : !state.doc ? btn('retry-document', 'Повторить загрузку') + btn('back', 'Вернуться к списку') : ''}</div>` : '';
  }
  function renderContent() {
    const content = document.getElementById('server-content'); if (!content) return;
    if (state.view === 'recovery') { content.innerHTML = messages() + renderRecovery(); return; }
    if (!state.company) { content.innerHTML = '<div class="server-empty"><h2>Нет доступных организаций</h2><p>Администратор должен предоставить доступ к организации и назначить рабочие роли.</p></div>'; return; }
    if (state.loading) { content.innerHTML = '<div class="server-loading" role="status">Загружаем данные сервера…</div>'; return; }
    content.innerHTML = messages() + (state.view === 'document' ? renderDocument() : state.view === 'list' ? renderList() : state.view === 'stock' ? renderStock() : state.view === 'exchange' ? renderExchange() : renderHome());
    const actionsBox = document.getElementById('server-actions'); if (actionsBox) actionsBox.innerHTML = actions();
  }
  function renderRecovery() {
    if (!state.unknown) return '';
    const pending = state.unknown;
    const name = pending.params.name || pending.params.document?.name;
    return `<section class="home-content server-recovery"><h1>Восстановление запроса</h1><dl><dt>Операция</dt><dd>${esc(commandLabels[pending.method] || 'Запись документа')}</dd><dt>Документ</dt><dd>${esc(specs[pending.params.kind]?.singular || '')}${name ? ` · ${esc(name)}` : ' · создание нового'}</dd><dt>Организация</dt><dd>${esc(state.company)}</dd>${pending.created_at ? `<dt>Запрос подготовлен</dt><dd>${esc(pending.created_at)}</dd>` : ''}</dl><p class="server-login-note">Это временный запрос для восстановления связи. Сохранение документа подтверждает сервер. Не создавайте документ заново до проверки результата.</p></section>`;
  }
  async function restoreRecovery() {
    try {
      const pending = await api.recovery();
      if (!pending) return false;
      if (!specs[pending.params.kind] || (pending.method === 'create_related' && !specs[pending.params.target_kind])) throw new ApiError('Вид документа незавершённой операции больше не поддерживается.', { code: 'RECOVERY_CORRUPT' });
      state.company = pending.company; state.kind = pending.params.kind; state.view = 'recovery'; state.doc = null; state.serverDocument = null; state.dirty = false; state.loading = false;
      state.unknown = { method: pending.method, params: clone(pending.params), successMessage: commandResults[pending.method], targetKind: pending.method === 'create_related' ? pending.params.target_kind : pending.params.kind, created_at: pending.created_at };
      state.error = 'Найден незавершённый запрос предыдущего открытия этой вкладки. Проверьте его результат тем же ключом.';
      state.recoveryIssue = false; shell(); setUrl(); return true;
    } catch (error) {
      state.view = 'recovery'; state.kind = ''; state.doc = null; state.serverDocument = null; state.error = error.message; state.recoveryIssue = true; state.loading = false; state.unknown = null; shell(); return true;
    }
  }
  function renderHome() {
    const steps = [['quotes', 'Подготовьте предложение'], ['orders', 'Зафиксируйте заказ'], ['deliveries', 'Отгрузите товары'], ['invoices', 'Выставьте счёт'], ['payments', 'Отразите оплату']].filter(([kind]) => capability(kind, 'read'));
    return `<section class="home-content server-welcome"><h1>Рабочее место</h1><p class="server-login-note">${esc(state.company)} · ${esc(state.session.full_name || state.session.user)}</p><div class="cards">${[['sales', 'Продажи', 'Заказы, отгрузки и счета покупателям'], ['purchase', 'Закупки', 'Заказы поставщикам и приёмка'], ['stock', 'Склад', 'Складские операции и инвентаризация']].filter(([module]) => Object.entries(specs).some(([kind, spec]) => spec.module === module && capability(kind, 'read'))).map(([module, label, description]) => `<a href="/erp" class="metric" data-module="${module}"><div class="metric-label">${icon(module)}</div><div class="metric-value">${label}</div><div class="metric-note">${description}</div></a>`).join('')}</div><h2>Продажа от предложения до оплаты</h2><p class="server-login-note">Создавайте следующий документ из проведённого предыдущего: сервер сохранит связи строк и рассчитает доступные количества и задолженность.</p><div class="server-flow">${steps.map(([kind, label], index) => `<a href="/erp?kind=${kind}" data-kind="${kind}"><span class="server-flow-number">${index + 1}</span><span><strong>${label}</strong><small>${specs[kind].title}</small></span></a>`).join('')}</div><p class="server-login-note">Остатки, стоимость и задолженность изменяются при серверном проведении документов. Списки показывают только данные выбранной организации, доступные вашей роли.</p></section>`;
  }
  function renderList() {
    const spec = specs[state.kind];
    const master = spec.master;
    const rows = state.rows.map(row => `<tr><td><a href="/erp?kind=${encodeURIComponent(state.kind)}&name=${encodeURIComponent(row.name)}" class="server-row-link" data-document="${esc(row.name)}">${esc(row.name)}</a>${master && (row.item_name || row.customer_name || row.supplier_name || row.warehouse_name) ? `<small>${esc(row.item_name || row.customer_name || row.supplier_name || row.warehouse_name)}</small>` : ''}</td><td>${esc(master ? (row.item_group || row.customer_group || row.supplier_group || row.parent_warehouse || '—') : (row.customer_name || row.customer || row.party_name || row.supplier_name || row.supplier || row.party || row.stock_entry_type || row.purpose || '—'))}</td>${master ? '' : `<td>${esc(row.transaction_date || row.posting_date || '—')}</td><td>${status(row)}${row.status ? `<small>${esc(row.status)}</small>` : ''}</td><td class="server-amount">${amount(row.grand_total ?? row.paid_amount ?? row.total_amount, state.kind === 'payments' ? row.paid_from_account_currency || '' : row.currency || currency(null))}</td>`}<td>${esc(String(row.modified || '').slice(0, 16))}</td></tr>`).join('');
    return `<section class="server-list"><form id="server-search-form" class="filterbar" role="search"><div class="filter search">${icon('search')}<input name="search" value="${esc(state.search)}" placeholder="Поиск по номеру и названию" aria-label="Поиск документов" autocomplete="off"></div><button type="submit" class="btn outline">Найти</button><label class="server-field-hint" for="server-page-size">На странице</label><select id="server-page-size" class="filter">${[25, 50, 100].map(size => `<option value="${size}" ${size === state.pageLength ? 'selected' : ''}>${size}</option>`).join('')}</select></form>${state.rows.length ? `<div class="table-scroller"><table><thead><tr><th>${master ? 'Название / код' : 'Номер'}</th><th>${master ? 'Группа' : 'Контрагент / операция'}</th>${master ? '' : '<th>Дата</th><th>Состояние</th><th class="server-amount">Сумма</th>'}<th>Изменён</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="server-empty"><h2>${state.error ? 'Список не загружен' : state.search ? 'Ничего не найдено' : 'Документов пока нет'}</h2><p>${state.error ? 'Повторите запрос, чтобы получить актуальные данные.' : state.search ? 'Измените запрос и попробуйте снова.' : capability(state.kind, 'create') ? 'Создайте первый документ в этой организации.' : 'Здесь будут документы, доступные вашей роли.'}</p></div>`}<div class="server-pagination"><span>${state.rows.length ? `${state.start + 1}–${state.start + state.rows.length}` : '0'} записей на этой странице</span><div class="server-page-buttons">${btn('page-prev', 'Назад', 'outline', state.start === 0 ? 'disabled' : '')}${btn('page-next', 'Далее', 'outline', state.hasMore ? '' : 'disabled')}</div></div></section>`;
  }
  function renderStock() {
    return `<section class="server-list"><form id="server-stock-filter" class="filterbar"><label class="server-field-hint" for="stock-warehouse">Склад</label><input class="filter" id="stock-warehouse" name="warehouse" value="${esc(state.warehouse)}" placeholder="Точное название или все склады"><button type="submit" class="btn outline">Показать</button></form><div class="server-notice">Остатки и стоимость получены из ERPNext. Резервы по заказам и складские резервы показаны отдельно.</div>${state.rows.length ? `<div class="table-scroller"><table><thead><tr><th>Номенклатура</th><th>Склад</th><th class="server-amount">Остаток</th><th class="server-amount">В заказах</th><th class="server-amount">Складские резервы</th><th class="server-amount">Учётная цена</th><th class="server-amount">Стоимость</th></tr></thead><tbody>${state.rows.map(row => `<tr><td>${esc(row.item_code)}</td><td>${esc(row.warehouse)}</td><td class="server-amount">${esc(row.actual_qty ?? '—')}</td><td class="server-amount">${esc(row.reserved_qty ?? '—')}</td><td class="server-amount">${esc(row.reserved_stock ?? '—')}</td><td class="server-amount">${amount(row.valuation_rate)}</td><td class="server-amount">${amount(row.stock_value)}</td></tr>`).join('')}</tbody></table></div>` : `<div class="server-empty">${state.error ? 'Не удалось загрузить остатки' : 'Нет складских данных по выбранным условиям'}</div>`}<div class="server-pagination"><span>${state.rows.length ? `${state.start + 1}–${state.start + state.rows.length}` : '0'} строк</span><div class="server-page-buttons">${btn('stock-prev', 'Назад', 'outline', state.start ? '' : 'disabled')}${btn('stock-next', 'Далее', 'outline', state.hasMore ? '' : 'disabled')}</div></div></section>`;
  }
  async function loadStock(start = 0) {
    state.kind = ''; state.view = 'stock'; state.doc = null; state.dirty = false; state.error = ''; state.rows = []; state.start = start; state.hasMore = false; state.loading = true;
    const id = ++state.requestId; shell(); setUrl();
    try {
      const result = await api.request('stock_balance', { company: state.company, warehouse: state.warehouse || undefined, start, page_length: state.pageLength });
      if (id !== state.requestId) return;
      if (!Array.isArray(result?.rows)) throw new ApiError('Неверный ответ отчёта по остаткам.');
      state.rows = result.rows; state.start = result.start; state.hasMore = Boolean(result.has_more);
    } catch (error) { if (id !== state.requestId) return; state.error = error.message; state.authIssue = needsLogin(error); }
    finally { if (id === state.requestId) { state.loading = false; renderContent(); } }
  }
  function exchangeStatus(value) {
    const labels = { Pending: 'В очереди', Sent: 'Отправлено', Applied: 'Применено', Stale: 'Устаревшее, пропущено', Processing: 'Обрабатывается', Retry: 'Ожидает повтора', Conflict: 'Конфликт', 'Needs Mapping': 'Нужно соответствие', Rejected: 'Отклонено', 'Dead Letter': 'Требует разбора', Paused: 'Приостановлено' };
    const tone = ['Sent', 'Applied'].includes(value) ? 'green' : ['Rejected', 'Conflict', 'Dead Letter'].includes(value) ? 'red' : 'orange';
    return `<span class="badge ${tone}">${esc(labels[value] || value)}</span>`;
  }
  function renderExchange() {
    const journal = state.exchange;
    const direction = state.exchangeDirection;
    const rows = journal?.[direction] || [];
    const kindByDoctype = { Quotation: 'quotes', 'Sales Order': 'orders', 'Delivery Note': 'deliveries', 'Sales Invoice': 'invoices', 'Purchase Order': 'purchaseOrders', 'Purchase Receipt': 'receipts', 'Purchase Invoice': 'purchaseInvoices', 'Payment Entry': 'payments', Item: 'items', Customer: 'customers', Supplier: 'suppliers' };
    const hints = { configure_mapping: journal?.can_manage_mappings ? 'Настройте соответствие в ERPNext и повторите проверку.' : 'Администратор должен настроить соответствие.', review_conflict: 'Проверьте более новую версию и причину конфликта перед повтором.' };
    return `<section class="server-list server-exchange"><div class="server-exchange-tabs" role="group" aria-label="Направление обмена">${btn('exchange-direction', `Входящие${journal ? ` · ${esc(journal.incoming_total)}` : ''}`, direction === 'incoming' ? 'primary' : 'outline', `data-direction="incoming" ${state.busy ? 'disabled' : ''}`)}${btn('exchange-direction', `Исходящие${journal ? ` · ${esc(journal.outgoing_total)}` : ''}`, direction === 'outgoing' ? 'primary' : 'outline', `data-direction="outgoing" ${state.busy ? 'disabled' : ''}`)}</div>${state.exchangeNotice ? `<div class="server-notice" role="status">${esc(state.exchangeNotice)}</div>` : ''}<div class="server-notice">Учётная операция и отправка сообщения выполняются отдельно. Статус «В очереди» означает ожидание отправки; доставку подтверждает статус сервера.</div>${rows.length ? `<div class="table-scroller"><table><thead><tr><th>Документ / сообщение</th><th>Направление</th><th>Состояние</th><th>Время изменения</th><th>Попытки</th><th>Причина / действие</th></tr></thead><tbody>${rows.map(row => {
      const kind = kindByDoctype[row.erp_doctype];
      const reference = kind && row.erp_name && capability(kind, 'read') ? `<a href="/erp?kind=${kind}&name=${encodeURIComponent(row.erp_name)}" data-linked-kind="${kind}" data-linked-name="${esc(row.erp_name)}">${esc(row.erp_name)}</a>` : esc(row.erp_name || row.object_id || row.message_id || row.name);
      return `<tr><td>${reference}<small>${esc(row.connector || '')} · ${esc(row.message_id || row.name)}</small></td><td>${direction === 'incoming' ? 'В ERP Basic' : 'В Clientora'}</td><td>${exchangeStatus(row.status)}</td><td>${esc(String(row.modified || row.creation || '').slice(0, 19))}</td><td>${esc(row.attempts ?? 0)}</td><td class="server-exchange-reason">${esc(row.error_message || '')}${hints[row.action] ? `<small>${esc(hints[row.action])}</small>` : ''}${row.can_retry && journal.can_retry ? btn('retry-exchange', ['Conflict', 'Needs Mapping', 'Rejected', 'Dead Letter'].includes(row.status) ? 'Повторить после проверки' : 'Повторить', 'outline', `data-direction="${direction}" data-name="${esc(row.name)}" ${state.busy ? 'disabled' : ''}`) : ''}</td></tr>`;
    }).join('')}</tbody></table></div>` : `<div class="server-empty">${state.error ? 'Журнал не загружен' : 'Сообщений в этом направлении пока нет'}</div>`}<div class="server-pagination"><span>${rows.length ? `${state.start + 1}–${state.start + rows.length}` : '0'} ${journal ? `из ${esc(journal[direction + '_total'])}` : ''}</span><div class="server-page-buttons">${btn('exchange-prev', 'Назад', 'outline', state.start && !state.busy ? '' : 'disabled')}${btn('exchange-next', 'Далее', 'outline', state.hasMore && !state.busy ? '' : 'disabled')}</div></div></section>`;
  }
  async function loadExchange(start = 0, preserveError = false) {
    state.kind = ''; state.view = 'exchange'; state.doc = null; state.dirty = false; if (!preserveError) { state.error = ''; state.exchangeNotice = ''; }
    state.exchange = null; state.start = start; state.hasMore = false; state.loading = true;
    const id = ++state.requestId; shell(); setUrl();
    try {
      const result = await api.request('journal', { company: state.company, start, page_length: state.pageLength }, { namespace: 'integration' });
      if (id !== state.requestId) return;
      if (!Array.isArray(result?.incoming) || !Array.isArray(result?.outgoing)) throw new ApiError('Сервер вернул неверный формат журнала обмена.');
      state.exchange = result; state.start = result.start; state.hasMore = Boolean(result[state.exchangeDirection + '_has_more']);
    } catch (error) { if (id !== state.requestId) return; state.error = error.message; state.authIssue = needsLogin(error); }
    finally { if (id === state.requestId) { state.loading = false; renderContent(); } }
  }
  async function retryExchange(direction, name) {
    if (state.busy || !['incoming', 'outgoing'].includes(direction)) return;
    const row = state.exchange?.[direction]?.find(item => item.name === name);
    if (!row?.can_retry || !state.exchange?.can_retry) return;
    if (!window.confirm(`Повторить обработку сообщения ${row.message_id || row.name}? Сервер проверит его текущее состояние и сохранит один бизнес-результат.`)) return;
    state.busy = true; state.error = ''; state.exchangeNotice = ''; renderContent();
    try {
      const result = await api.request(direction === 'incoming' ? 'retry_inbox' : 'retry_outbox', { company: state.company, name }, { namespace: 'integration', verb: 'POST', mutation: true });
      if (!result || !result.status) throw new ApiError('Результат повтора не подтверждён. Обновите журнал.', { uncertain: true });
      state.exchangeNotice = direction === 'outgoing' ? (result.status === 'Sent' ? 'Сервер подтверждает отправку сообщения.' : 'Сообщение возвращено в очередь. Доставка пока не подтверждена.') : result.status === 'Applied' ? 'Сервер подтвердил обработку входящего сообщения.' : result.status === 'Stale' ? 'Сервер пропустил устаревшее сообщение.' : 'Состояние сообщения обновлено на сервере.';
    } catch (error) { state.error = error.message; state.authIssue = needsLogin(error); }
    finally { state.busy = false; await loadExchange(state.start, true); }
  }
  function getPath(path) { return path.split('.').reduce((value, key) => value?.[key], state.doc); }
  function setPath(path, value) {
    const parts = path.split('.'); let object = state.doc;
    for (const part of parts.slice(0, -1)) { if (!object[part]) object[part] = {}; object = object[part]; }
    object[parts.at(-1)] = value;
  }
  function fieldHtml(field, path = field.name, compact = false) {
    const value = getPath(path) ?? '';
    const id = `field-${path.replace(/\./g, '-')}`;
    const attrs = `id="${id}" data-field="${esc(path)}" ${field.required ? 'required' : ''}`;
    let input;
    if (field.type === 'checkbox') input = `<input type="checkbox" ${attrs} ${value ? 'checked' : ''}>`;
    else if (field.type === 'select') input = `<select ${attrs}>${field.required ? '' : '<option value="">—</option>'}${field.values.map(([val, label]) => `<option value="${esc(val)}" ${String(value) === val ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select>`;
    else if (field.type === 'reference') input = `<div class="server-reference-control"><input type="text" ${attrs} value="${esc(value)}" autocomplete="off" placeholder="${compact ? 'Выберите…' : ''}">${specs[field.lookup] || field.lookup === 'party' || additionalLookups.has(field.lookup) ? btn('lookup', icon('search'), 'icon-btn', `data-lookup="${esc(field.lookup)}" data-target="${esc(path)}" aria-label="Выбрать: ${esc(field.label)}"`) : ''}</div>`;
    else input = `<input type="${field.type}" ${attrs} value="${esc(value)}" ${field.type === 'number' ? `step="any" ${field.min !== undefined ? `min="${field.min}"` : ''} ${field.max !== undefined ? `max="${field.max}"` : ''}` : ''}>`;
    return compact ? input : `<div class="field"><label for="${id}">${esc(field.label)}${field.required ? ' *' : ''}</label>${input}</div>`;
  }
  function itemColumns() {
    const type = specs[state.kind].items;
    const columns = [ref('item_code', 'Номенклатура', 'items', { required: true }), number('qty', type === 'inventory' ? 'Фактическое количество' : 'Количество', { required: true, min: state.doc.is_return ? undefined : type === 'inventory' ? 0 : 0.000001 })];
    if (type === 'inventory') return [...columns, number('valuation_rate', 'Учётная стоимость', { required: true, min: 0 }), ref('warehouse', 'Склад', 'warehouses', { required: true })];
    if (type === 'trade') columns.push(number('rate', 'Цена', { required: true, min: 0 }));
    columns.push(ref('uom', 'Ед. изм.', 'uoms'));
    if (type === 'stock') {
      const purpose = state.doc.stock_entry_type;
      if (purpose !== 'Material Receipt') columns.push(ref('s_warehouse', 'Со склада', 'warehouses', { required: true }));
      if (purpose !== 'Material Issue') columns.push(ref('t_warehouse', 'На склад', 'warehouses', { required: true }));
      if (purpose === 'Material Receipt') columns.push(number('basic_rate', 'Учётная цена', { min: 0 }));
    } else columns.push(ref('warehouse', 'Склад', 'warehouses'));
    return columns;
  }
  function renderItems(editable) {
    const columns = itemColumns();
    return `<section class="form-section"><h2>Товары и услуги</h2><div class="server-doc-table"><table><thead><tr><th>№</th>${columns.map(field => `<th>${esc(field.label)}</th>`).join('')}${editable ? '<th></th>' : ''}</tr></thead><tbody>${(state.doc.items || []).map((item, index) => `<tr><td>${index + 1}</td>${columns.map(field => `<td class="${field.name === 'item_code' ? 'server-item-cell' : field.type === 'number' ? 'server-qty-cell' : ''}">${fieldHtml(field, `items.${index}.${field.name}`, true)}${field.name === 'item_code' && item.item_name ? `<small class="server-item-hint">${esc(item.item_name)}</small>` : ''}</td>`).join('')}${editable ? `<td class="server-remove-cell">${btn('remove-item', icon('close'), 'icon-btn', `data-index="${index}" aria-label="Удалить строку ${index + 1}"`)}</td>` : ''}</tr>`).join('')}</tbody></table></div>${editable ? btn('add-item', icon('plus') + ' Добавить строку') : ''}</section>`;
  }
  function renderReferences(editable) {
    return `<section class="form-section"><h2>Назначение оплаты</h2><p class="server-field-hint">Связи и остатки заполняются сервером при создании платежа из счёта. Для частичной оплаты измените распределённую сумму.</p><div class="server-doc-table"><table><thead><tr><th>Тип</th><th>Документ</th><th>Остаток по данным сервера</th><th>Распределено</th></tr></thead><tbody>${(state.doc.references || []).map((row, index) => `<tr><td>${esc(row.reference_doctype)}</td><td>${esc(row.reference_name)}</td><td class="server-amount">${amount(row.outstanding_amount, paymentCurrency('allocated_amount', state.doc))}</td><td>${fieldHtml(number('allocated_amount', 'Распределённая сумма', { required: true, min: 0 }), `references.${index}.allocated_amount`, true)}</td></tr>`).join('') || '<tr><td colspan="4">Без привязки к счёту</td></tr>'}</tbody></table></div></section>`;
  }
  function renderDocument() {
    if (!state.doc) return '';
    const spec = specs[state.kind];
    const editable = Number(state.doc.docstatus || 0) === 0 && docPermission(state.doc.name ? 'write' : 'create');
    const locked = !editable || state.busy || state.unknown;
    const doc = state.doc;
    const serverDocument = doc.name ? state.serverDocument || {} : {};
    const mapped = doc.name && Number(doc.docstatus) === 1 && !doc.is_return && !state.dirty && !state.unknown ? (spec.maps || []).filter(([kind]) => capability(kind, 'create')) : [];
    const totals = spec.master ? [] : state.kind === 'payments' ? [['paid_amount', 'Со счёта'], ['received_amount', 'На счёт'], ['total_allocated_amount', 'Распределено'], ['unallocated_amount', 'Нераспределено']] : [['net_total', 'Без налогов'], ['total_taxes_and_charges', 'Налоги и сборы'], ['grand_total', 'Итого'], ['outstanding_amount', 'Остаток к оплате']];
    return `<article class="server-document"><div class="server-document-footer"><div>${doc.name ? status(doc) : '<span class="badge orange">Новый документ</span>'} <span class="server-readonly">${esc(doc.status || '')}</span></div><span class="server-save-status">${state.busy ? 'Ожидаем подтверждение сервера…' : state.dirty ? '<span class="server-dirty">Изменения не сохранены</span>' : doc.name ? `Сохранён ${esc(String(doc.modified || '').slice(0, 19))}` : 'Ещё не сохранён'}</span></div>${doc.is_return ? `<div class="server-notice">Возврат по документу ${esc(doc.return_against || '')}. Количества возврата отрицательные; сервер проверит доступное количество и учётную стоимость.</div>` : ''}${editable ? '' : '<div class="server-notice">Документ открыт для просмотра. Изменения и проведение доступны только в разрешённом состоянии и с соответствующими правами.</div>'}<form id="server-document-form"><fieldset ${locked ? 'disabled' : ''}><section class="form-section"><div class="form-grid">${spec.fields.map(field => fieldHtml(field)).join('')}</div>${state.kind === 'invoices' || state.kind === 'purchaseInvoices' ? '<p class="server-field-hint">Складское движение оформляется отдельной отгрузкой или приёмкой. Этот счёт отражает расчёты с контрагентом.</p>' : ''}</section>${spec.items ? renderItems(editable) : ''}${state.kind === 'payments' ? renderReferences(editable) : ''}</fieldset></form>${totals.some(([key]) => serverDocument[key] !== undefined) ? `<dl class="server-summary">${totals.filter(([key]) => serverDocument[key] !== undefined).map(([key, label]) => `<div><dt>${label}</dt><dd>${amount(serverDocument[key], state.kind === 'payments' ? paymentCurrency(key, serverDocument) : currency())}</dd></div>`).join('')}<p class="server-summary-note">${state.dirty ? 'Показаны суммы последней серверной версии; новые суммы появятся после сохранения.' : 'Суммы рассчитаны сервером.'}</p></dl>` : !spec.master ? '<div class="server-notice">Сервер рассчитает суммы и проверит строки при сохранении.</div>' : ''}${mapped.length ? `<section class="form-section"><h2>Создать на основании</h2><div class="actions">${mapped.map(([kind, label]) => btn('map-document', label, 'outline', `data-target-kind="${kind}" ${state.busy ? 'disabled' : ''}`)).join('')}</div><p class="server-field-hint">ERPNext свяжет документы и рассчитает доступное количество или сумму. Связанный черновик создаётся на сервере. Проверьте его, сохраните изменения и проведите.</p></section>` : ''}${doc.name && Number(doc.docstatus) === 1 && ['deliveries', 'receipts', 'invoices', 'purchaseInvoices'].includes(state.kind) && !doc.is_return && docPermission('create') ? `<section class="form-section">${btn('create-return', 'Создать возврат', 'outline', state.busy || state.unknown ? 'disabled' : '')}</section>` : ''}${renderSourceLinks()}${doc.name ? `<section class="form-section"><h2>История изменений</h2><div class="actions">${btn('audit', 'История изменений', 'outline', state.busy || state.unknown ? 'disabled' : '')}</div><div id="server-document-details"></div></section>` : ''}</article>`;
  }
  function renderSourceLinks() {
    if (!state.doc) return '';
    const references = new Map();
    const fields = { sales_order: 'orders', against_sales_order: 'orders', delivery_note: 'deliveries', purchase_order: 'purchaseOrders', purchase_receipt: 'receipts' };
    for (const item of state.doc.items || []) for (const [field, kind] of Object.entries(fields)) if (item[field] && capability(kind, 'read')) references.set(`${kind}:${item[field]}`, { kind, name: item[field] });
    if (state.doc.return_against) references.set(`return:${state.doc.return_against}`, { kind: state.kind, name: state.doc.return_against });
    const nativeKinds = { 'Sales Invoice': 'invoices', 'Sales Order': 'orders', 'Purchase Invoice': 'purchaseInvoices', 'Purchase Order': 'purchaseOrders' };
    for (const row of state.doc.references || []) if (nativeKinds[row.reference_doctype] && capability(nativeKinds[row.reference_doctype], 'read')) references.set(`payment:${row.reference_name}`, { kind: nativeKinds[row.reference_doctype], name: row.reference_name });
    return references.size ? `<section class="form-section"><h2>Документы-основания</h2><div class="actions">${Array.from(references.values()).map(row => `<a class="btn outline" href="/erp?kind=${encodeURIComponent(row.kind)}&name=${encodeURIComponent(row.name)}" data-linked-kind="${row.kind}" data-linked-name="${esc(row.name)}">${esc(row.name)}</a>`).join('')}</div></section>` : '';
  }
  function collectDocument() {
    const spec = specs[state.kind]; const doc = {};
    if (state.doc.name) doc.name = state.doc.name;
    for (const field of spec.fields) { const value = state.doc[field.name]; if (value !== undefined) doc[field.name] = value; }
    for (const [key, value] of Object.entries(spec.defaults || {})) if (doc[key] === undefined) doc[key] = value;
    for (const key of ['remarks', 'currency', 'conversion_rate', 'set_warehouse', 'selling_price_list', 'buying_price_list']) {
      if (state.doc[key] !== undefined && !spec.master && !['stockDocs', 'inventory', 'payments'].includes(state.kind)) doc[key] = state.doc[key];
    }
    if (state.kind === 'stockDocs') { doc.stock_entry_type = state.doc.stock_entry_type; doc.purpose = state.doc.stock_entry_type; }
    if (spec.items) {
      const writable = spec.items === 'stock' ? stockChildFields : spec.items === 'inventory' ? inventoryChildFields : tradeChildFields;
      doc.items = (state.doc.items || []).map(item => Object.fromEntries(writable.filter(key => item[key] !== undefined).map(key => [key, item[key]])));
    }
    if (state.kind === 'payments') {
      for (const key of ['source_exchange_rate', 'target_exchange_rate']) if (state.doc[key] !== undefined) doc[key] = state.doc[key];
      doc.references = (state.doc.references || []).map(row => Object.fromEntries(['name', 'reference_doctype', 'reference_name', 'allocated_amount'].filter(key => row[key] !== undefined).map(key => [key, row[key]])));
    }
    return doc;
  }
  function leaveAllowed() {
    if (state.busy) { toast('Дождитесь ответа сервера.'); return false; }
    if (state.recoveryIssue) { toast('Сначала проверьте незавершённую операцию или выйдите из системы.'); return false; }
    if (state.unknown) { toast('Сначала подтвердите результат предыдущей операции повтором того же запроса.'); return false; }
    return !state.dirty || window.confirm('Есть несохранённые изменения. Перейти и потерять эти изменения?');
  }
  async function loadList(kind, { start = 0, search = state.search } = {}) {
    if (!specs[kind] || !capability(kind, 'read')) { state.error = 'Нет доступа к этому разделу.'; renderContent(); return; }
    state.kind = kind; state.view = 'list'; state.doc = null; state.dirty = false; state.error = ''; state.rows = [];
    state.start = start; state.search = search; state.hasMore = false; state.loading = true;
    const id = ++state.requestId; const company = state.company;
    shell(); setUrl();
    try {
      const result = await api.request('list_documents', { kind, company, start, page_length: state.pageLength, search });
      if (id !== state.requestId) return;
      if (!result || !Array.isArray(result.rows)) throw new ApiError('Сервер вернул неверный формат списка.');
      state.rows = result.rows; state.start = result.start; state.hasMore = Boolean(result.has_more);
    } catch (error) { if (id !== state.requestId) return; state.error = error.message; state.authIssue = needsLogin(error); }
    finally { if (id === state.requestId) { state.loading = false; renderContent(); } }
  }
  async function loadDocument(name, kind = state.kind) {
    state.kind = kind; state.requestedName = name; state.view = 'document'; state.doc = null; state.dirty = false; state.error = ''; state.loading = true;
    const id = ++state.requestId; const company = state.company; shell();
    try {
      const response = await api.request('get_document', { kind, name, company });
      if (id !== state.requestId) return;
      acceptDocument(response);
    } catch (error) { if (id !== state.requestId) return; state.error = error.message; state.authIssue = needsLogin(error); }
    finally { if (id === state.requestId) { state.loading = false; shell(); setUrl(); } }
  }
  function validateDocumentResponse(response, expectedKind = state.kind) {
    if (!response?.document || typeof response.document !== 'object' || Array.isArray(response.document) || response.kind !== expectedKind || response.company !== state.company || (state.busy && (!response.document.name || !response.document.modified))) throw new ApiError('Не удалось подтвердить документ и организацию в ответе сервера.', { code: 'INVALID_DOCUMENT', uncertain: state.busy });
  }
  function acceptDocument(response) {
    validateDocumentResponse(response);
    state.doc = clone(response.document); state.serverDocument = clone(response.document); state.permissions = response.permissions || {}; state.dirty = false; state.error = ''; state.unknown = null; state.authIssue = false; state.recoveryIssue = false;
  }
  function newDocument(kind = state.kind) {
    if (!capability(kind, 'create')) return;
    state.kind = kind; state.view = 'document'; state.doc = clone(specs[kind].defaults || {}); state.serverDocument = null; state.permissions = state.session.capabilities[kind]; state.error = ''; state.dirty = false; state.loading = false;
    for (const field of specs[kind].fields) if (['posting_date', 'transaction_date'].includes(field.name)) state.doc[field.name] = today();
    if (specs[kind].items) state.doc.items = [{ qty: 1 }];
    shell(); setUrl();
  }
  async function executeCommand(method, params, successMessage, targetKind = state.kind) {
    if (state.busy) return;
    state.busy = true; state.error = ''; shell();
    try {
      const result = await api.command(method, params, { validate: result => validateDocumentResponse(result, targetKind) });
      state.kind = targetKind; state.view = 'document'; acceptDocument(result); toast(successMessage); setUrl();
    } catch (error) {
      state.error = error.message; state.authIssue = needsLogin(error);
      if (['RECOVERY_CORRUPT', 'RECOVERY_ACCESS_CHANGED'].includes(error.code)) { state.recoveryIssue = true; state.unknown = null; state.view = 'recovery'; }
      else if (error.uncertain) state.unknown = { method, params: clone(params), successMessage, targetKind };
      else state.unknown = null;
    } finally { state.busy = false; shell(); }
  }
  async function save() {
    if (!state.doc || state.busy || state.unknown) return;
    const form = document.getElementById('server-document-form'); if (!form.reportValidity()) return;
    if (specs[state.kind].items && !state.doc.items?.length) { state.error = 'Добавьте хотя бы одну строку документа.'; renderContent(); return; }
    const params = { kind: state.kind, company: state.company, document: collectDocument() };
    if (state.doc.name) {
      if (!state.doc.modified) { state.error = 'Версия документа неизвестна. Загрузите актуальный документ перед сохранением.'; renderContent(); return; }
      params.expected_modified = state.doc.modified;
    }
    await executeCommand('save_document', params, 'Документ сохранён на сервере.');
  }
  async function transition(method) {
    if (!state.doc?.name || state.dirty || state.busy || state.unknown || state.recoveryIssue) return;
    if (!state.doc.modified) { state.error = 'Версия документа неизвестна. Загрузите актуальный документ.'; renderContent(); return; }
    const submitting = method === 'submit_document';
    if (!window.confirm(`${submitting ? 'Провести' : 'Отменить'} документ ${state.doc.name}? ${submitting ? 'ERPNext проверит документ и выполнит учётные операции.' : 'ERPNext проверит связанные документы и выполнит обратные учётные операции.'}`)) return;
    await executeCommand(method, { kind: state.kind, name: state.doc.name, company: state.company, expected_modified: state.doc.modified }, submitting ? 'Документ проведён на сервере.' : 'Документ отменён на сервере.');
  }
  async function mapDocument(targetKind) {
    if (!state.doc?.name || state.busy || state.unknown || state.dirty) return;
    if (targetKind !== 'payments') {
      return executeCommand('create_related', { kind: state.kind, name: state.doc.name, company: state.company, target_kind: targetKind, expected_modified: state.doc.modified }, 'Связанный черновик создан на сервере.', targetKind);
    }
    state.busy = true; state.error = ''; renderContent();
    try {
      const response = await api.request('payment_template', { kind: state.kind, name: state.doc.name, company: state.company });
      if (!response?.document || typeof response.document !== 'object' || response.saved !== false || response.document.name) throw new ApiError('Сервер вернул неподходящий шаблон оплаты.');
      state.kind = 'payments'; state.view = 'document'; state.doc = clone(response.document); state.serverDocument = null; state.permissions = state.session.capabilities.payments; state.dirty = true;
    } catch (error) { state.error = error.message; state.authIssue = needsLogin(error); }
    finally { state.busy = false; shell(); setUrl(); }
  }
  async function createReturn() {
    if (!state.doc?.name || state.busy || state.unknown || state.dirty || state.doc.is_return) return;
    if (!window.confirm(`Создать черновик возврата по ${state.doc.name}? Проверьте строки и количество перед проведением.`)) return;
    return executeCommand('create_return', { kind: state.kind, name: state.doc.name, company: state.company, expected_modified: state.doc.modified }, 'Черновик возврата создан на сервере.');
  }
  async function showDocumentDetails(type) {
    const box = document.getElementById('server-document-details'); if (!box) return;
    box.innerHTML = '<div class="server-loading">Загружаем…</div>';
    const id = state.requestId; const kind = state.kind; const name = state.doc.name;
    try {
      const response = await api.request('list_audit', { company: state.company, reference_name: name, start: 0, page_length: 25 });
      if (id !== state.requestId || name !== state.doc?.name || kind !== state.kind) return;
      const rows = response.rows || response.links || [];
      if (!Array.isArray(rows)) throw new ApiError('Сервер вернул неверный формат истории.');
      box.innerHTML = `<div class="server-journal">${rows.length ? rows.map(row => type === 'audit' ? `<p><strong>${esc(row.action || row.operation || row.type || '')}</strong> · ${esc(row.actor || row.user || row.owner || '')}<br><small>${esc(row.creation || row.timestamp || '')}</small></p>` : `<p>${specs[row.kind] && capability(row.kind, 'read') ? `<a href="/erp?kind=${encodeURIComponent(row.kind)}&name=${encodeURIComponent(row.name)}" data-linked-kind="${esc(row.kind)}" data-linked-name="${esc(row.name)}">${esc(specs[row.kind].singular)} · ${esc(row.name)}</a>` : esc(row.name || row.label || '')}</p>`).join('') : '<p class="server-field-hint">Записей нет.</p>'}${response.has_more ? '<p class="server-field-hint">Показаны последние 25 записей. Полная история доступна администратору в ERPNext.</p>' : ''}</div>`;
    } catch (error) { if (id === state.requestId) box.innerHTML = `<div class="server-error">${esc(error.message)}</div>`; }
  }
  async function openLookup(kind, path) {
    if (state.busy || state.unknown) return;
    if (kind === 'party') kind = state.doc.party_type === 'Supplier' ? 'suppliers' : 'customers';
    state.lookup = { kind, path, search: '', start: 0, pageLength: 25, rows: [], hasMore: false, loading: false, error: '', requestId: 0 };
    renderLookup(); dialog.showModal(); await loadLookup();
  }
  function renderLookup() {
    const lookup = state.lookup; if (!lookup) return;
    const title = specs[lookup.kind]?.title || ({ accounts: 'Счета', uoms: 'Единицы измерения', itemGroups: 'Группы номенклатуры', customerGroups: 'Группы покупателей', supplierGroups: 'Группы поставщиков', territories: 'Территории', priceLists: 'Прайс-листы', stockEntryTypes: 'Складские операции' })[lookup.kind] || 'Выбор значения';
    dialog.innerHTML = `<div class="dialog-head"><h2>${esc(title)}</h2>${btn('close-dialog', icon('close'), 'icon-btn', 'aria-label="Закрыть"')}</div><form id="server-lookup-search" class="filterbar" role="search"><input name="search" value="${esc(lookup.search)}" placeholder="Введите код или название" aria-label="Поиск в справочнике"><button class="btn outline" type="submit">Найти</button></form>${lookup.loading ? '<div class="server-loading">Загружаем справочник…</div>' : lookup.error ? `<div class="server-error">${esc(lookup.error)}</div>` : `<div class="server-lookup-rows">${lookup.rows.map((row, index) => `<button type="button" data-action="select-reference" data-index="${index}"><strong>${esc(row.item_name || row.customer_name || row.supplier_name || row.warehouse_name || row.account_name || row.label || row.name)}</strong><small>${esc(row.name)}${row.stock_uom ? ` · ${esc(row.stock_uom)}` : row.account_currency || row.currency ? ` · ${esc(row.account_currency || row.currency)}` : ''}</small></button>`).join('') || '<div class="server-empty">Ничего не найдено</div>'}</div>`}<div class="server-pagination"><span>${lookup.rows.length ? lookup.start + 1 + '–' + (lookup.start + lookup.rows.length) : 0}</span><div class="server-page-buttons">${btn('lookup-prev', 'Назад', 'outline', !lookup.start || lookup.loading ? 'disabled' : '')}${btn('lookup-next', 'Далее', 'outline', !lookup.hasMore || lookup.loading ? 'disabled' : '')}</div></div>`;
  }
  async function loadLookup() {
    const lookup = state.lookup; if (!lookup) return;
    const id = ++lookup.requestId; lookup.loading = true; lookup.error = ''; lookup.rows = []; renderLookup();
    try {
      const result = await api.request(additionalLookups.has(lookup.kind) ? 'lookup' : 'list_documents', { kind: lookup.kind, company: state.company, start: lookup.start, page_length: lookup.pageLength, search: lookup.search });
      if (state.lookup !== lookup || id !== lookup.requestId) return;
      if (!Array.isArray(result?.rows)) throw new ApiError('Неверный ответ справочника.');
      lookup.rows = result.rows; lookup.hasMore = Boolean(result.has_more);
    } catch (error) { if (state.lookup === lookup && id === lookup.requestId) lookup.error = error.message; }
    finally { if (state.lookup === lookup && id === lookup.requestId) { lookup.loading = false; renderLookup(); } }
  }
  function selectReference(index) {
    const lookup = state.lookup; const row = lookup?.rows[index]; if (!row) return;
    const previous = getPath(lookup.path);
    setPath(lookup.path, row.name);
    if (lookup.kind === 'items' && previous !== row.name && /^items\.\d+\.item_code$/.test(lookup.path)) {
      const item = state.doc.items[Number(lookup.path.split('.')[1])];
      item.item_name = row.item_name || ''; item.uom = null; item.conversion_factor = null;
    }
    if (lookup.kind === 'uoms' && previous !== row.name && /^items\.\d+\.uom$/.test(lookup.path)) state.doc.items[Number(lookup.path.split('.')[1])].conversion_factor = null;
    state.dirty = true; closeDialog(); renderContent();
    document.getElementById(`field-${lookup.path.replace(/\./g, '-')}`)?.focus();
  }
  function closeDialog() {
    const password = dialog.querySelector('input[type=password]'); if (password) password.value = '';
    dialog.close(); state.lookup = null;
  }
  function changeTheme() {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('erpbasic-theme', next); } catch (_) { /* Тема — единственная постоянная локальная настройка. */ }
    const meta = document.querySelector('meta[name="theme-color"]'); if (meta) meta.content = next === 'dark' ? '#191d23' : '#f8f8f8';
  }
  function needsLogin(error) { return error.status === 401 || ['AuthenticationError', 'SessionExpired', 'CSRFTokenError'].includes(error.code); }
  function reauthenticate() {
    if (state.busy || !state.session) return;
    state.lookup = null;
    dialog.innerHTML = `<div class="dialog-head"><h2>Повторный вход</h2>${btn('close-dialog', icon('close'), 'icon-btn', 'aria-label="Закрыть"')}</div><form id="server-reauth-form" class="dialog-body"><p class="server-field-hint">Документ и ключ незавершённой операции останутся на этой странице.</p><div class="field"><label for="reauth-user">Пользователь</label><input id="reauth-user" name="username" value="${esc(state.session.user)}" readonly autocomplete="username"></div><div class="field"><label for="reauth-password">Пароль</label><input id="reauth-password" name="password" type="password" required autocomplete="current-password"></div><div id="reauth-error"></div><button type="submit" class="btn primary">Войти</button></form>`;
    dialog.showModal(); dialog.querySelector('[name=password]').focus();
  }
  async function submitReauthentication(form) {
    const button = form.querySelector('button[type=submit]'); button.disabled = true;
    try {
      const user = state.session.user;
      const session = await api.login(user, form.elements.password.value);
      form.elements.password.value = '';
      if (!session?.authenticated || session.user !== user) throw new ApiError('Вход не подтверждён для текущего пользователя.');
      if (!session.companies?.some(company => company.name === state.company)) {
        closeDialog(); state.unknown = null; state.doc = null; state.rows = []; state.dirty = false; state.kind = ''; state.view = 'home';
        state.session = session; state.company = session.companies?.[0]?.name || '';
        if (await restoreRecovery()) return;
        shell(); setUrl(); return;
      }
      state.session = session; state.authIssue = false; state.error = state.unknown ? 'Сессия восстановлена. Повторите тот же запрос, чтобы получить сохранённый результат.' : '';
      closeDialog(); shell(); toast('Сессия восстановлена.');
    } catch (error) {
      const errorBox = document.getElementById('reauth-error');
      if (errorBox) errorBox.innerHTML = `<div class="server-error">${esc(error.message)}</div>`;
      else { state.error = error.message; renderContent(); }
      button.disabled = false;
    }
  }
  async function logout() {
    if (state.busy) return;
    if (state.unknown || state.recoveryIssue) {
      if (!window.confirm('Результат предыдущей операции не подтверждён. Безопаснее повторить тот же запрос. При выходе служебная запись восстановления будет удалена: после нового входа сначала найдите созданный документ на сервере и только затем вводите его снова. Всё равно выйти?')) return;
    } else if (!leaveAllowed()) return;
    state.busy = true; shell();
    try { await api.logout(); ++state.requestId; state.unknown = null; showLogin(); }
    catch (error) {
      if (!api.user) showLogin('Вы вышли из системы, но браузер не позволил удалить служебную запись восстановления. Очистите данные этого сайта перед передачей устройства другому человеку.');
      else state.error = error.message;
    } finally { state.busy = false; if (state.session) shell(); }
  }
  async function start() {
    const id = ++state.requestId;
    try {
      const session = await api.session(); if (id !== state.requestId) return;
      if (!session?.authenticated) { showLogin(); return; }
      if (!Array.isArray(session.companies)) throw new ApiError('API сессии вернул неверный список организаций.');
      state.session = session; const params = new URLSearchParams(location.search);
      state.company = session.companies.find(row => row.name === params.get('company'))?.name || session.companies[0]?.name || '';
      if (await restoreRecovery()) return;
      if (id !== state.requestId) return;
      if (!state.company) { shell(); return; }
      const kind = params.get('kind'); const name = params.get('name');
      if (params.get('view') === 'exchange' && canExchange()) { await loadExchange(); return; }
      if (params.get('view') === 'stock') { await loadStock(); return; }
      if (kind && specs[kind] && capability(kind, 'read')) { if (name) await loadDocument(name, kind); else await loadList(kind, { search: '' }); }
      else { state.view = 'home'; state.kind = ''; shell(); setUrl(); }
    } catch (error) {
      if (id !== state.requestId) return;
      app.innerHTML = `<main id="content" class="server-start"><h1>ERP Basic</h1><div class="server-error" role="alert"><p>${esc(error.message)}</p>${btn('bootstrap', 'Повторить подключение')}</div><p class="server-login-note">Данные учёта доступны после подключения к серверу.</p></main>`;
    }
  }
  function setMenu(open) {
    const sidebar = document.querySelector('.sidebar');
    if (!sidebar) return;
    const visible = open === undefined ? !sidebar.classList.contains('open') : open;
    sidebar.classList.toggle('open', visible);
    document.querySelector('.mobile-overlay')?.classList.toggle('visible', visible);
    document.querySelector('[data-action="menu"]')?.setAttribute('aria-expanded', String(visible));
  }
  document.addEventListener('keydown', event => { if (event.key === 'Escape') setMenu(false); });
  document.addEventListener('click', async event => {
    const target = event.target.closest('a,button'); if (!target || !app.contains(target) && !dialog.contains(target)) return;
    if (target.dataset.nav || target.dataset.module || target.dataset.kind || target.dataset.document || target.dataset.linkedKind) {
      event.preventDefault(); if (!leaveAllowed()) return;
      if (target.dataset.document) return loadDocument(target.dataset.document);
      if (target.dataset.linkedKind) return loadDocument(target.dataset.linkedName, target.dataset.linkedKind);
      if (target.dataset.kind) return loadList(target.dataset.kind, { search: '' });
      const module = target.dataset.module || 'home';
      if (module === 'home') { ++state.requestId; state.view = 'home'; state.kind = ''; state.doc = null; state.dirty = false; state.error = ''; state.loading = false; shell(); setUrl(); return; }
      if (module === 'exchange' && canExchange()) return loadExchange();
      const kind = Object.keys(specs).find(kind => specs[kind].module === module && capability(kind, 'read'));
      if (kind) return loadList(kind, { search: '' }); return;
    }
    const action = target.dataset.action; if (!action) return;
    if (target.tagName === 'A') event.preventDefault();
    if (target.disabled) return;
    if (action === 'theme') return changeTheme();
    if (action === 'menu') return setMenu();
    if (action === 'close-menu') return setMenu(false);
    if (action === 'bootstrap') return start();
    if (action === 'reauth') return reauthenticate();
    if (action === 'retry-document' && state.requestedName) return loadDocument(state.requestedName);
    if (action === 'close-dialog') return closeDialog();
    if (action === 'logout') return logout();
    if (action === 'exchange-journal' && leaveAllowed()) return loadExchange();
    if (action === 'refresh-exchange' && !state.busy) return loadExchange(state.start);
    if (action === 'exchange-prev') return loadExchange(Math.max(0, state.start - state.pageLength));
    if (action === 'exchange-next') return loadExchange(state.start + state.pageLength);
    if (action === 'exchange-direction' && !state.busy) { state.exchangeDirection = target.dataset.direction; return loadExchange(); }
    if (action === 'retry-exchange') return retryExchange(target.dataset.direction, target.dataset.name);
    if (action === 'stock-report' && leaveAllowed()) { event.preventDefault(); return loadStock(); }
    if (action === 'refresh-stock') return loadStock(state.start);
    if (action === 'stock-prev') return loadStock(Math.max(0, state.start - state.pageLength));
    if (action === 'stock-next') return loadStock(state.start + state.pageLength);
    if (action === 'new' && leaveAllowed()) return newDocument();
    if (action === 'back' && leaveAllowed()) return loadList(state.kind);
    if (action === 'refresh') { if (state.view === 'list') return loadList(state.kind, { start: state.start }); if (state.view === 'stock') return loadStock(state.start); if (state.view === 'exchange') return loadExchange(state.start); return start(); }
    if (action === 'page-prev') return loadList(state.kind, { start: Math.max(0, state.start - state.pageLength) });
    if (action === 'page-next') return loadList(state.kind, { start: state.start + state.pageLength });
    if (action === 'reload-document' && leaveAllowed()) return loadDocument(state.doc.name);
    if (action === 'save') return save();
    if (action === 'submit') return transition('submit_document');
    if (action === 'cancel') return transition('cancel_document');
    if (action === 'retry-command' && state.unknown) { const { method, params, successMessage, targetKind } = state.unknown; return executeCommand(method, params, successMessage, targetKind); }
    if (action === 'map-document') return mapDocument(target.dataset.targetKind);
    if (action === 'create-return') return createReturn();
    if (action === 'audit' || action === 'links') return showDocumentDetails(action);
    if (action === 'add-item') { state.doc.items.push({ qty: 1 }); state.dirty = true; return renderContent(); }
    if (action === 'remove-item') { state.doc.items.splice(Number(target.dataset.index), 1); state.dirty = true; return renderContent(); }
    if (action === 'lookup') return openLookup(target.dataset.lookup, target.dataset.target);
    if (action === 'select-reference') return selectReference(Number(target.dataset.index));
    if (action === 'lookup-prev' || action === 'lookup-next') { state.lookup.start = Math.max(0, state.lookup.start + (action === 'lookup-prev' ? -1 : 1) * state.lookup.pageLength); return loadLookup(); }
  });
  document.addEventListener('submit', async event => {
    if (event.target.id === 'server-reauth-form') {
      event.preventDefault(); return submitReauthentication(event.target);
    } else if (event.target.id === 'server-login-form') {
      event.preventDefault(); const form = event.target; const username = form.elements.username.value;
      const button = form.querySelector('button[type="submit"]'); button.disabled = true; button.textContent = 'Входим…';
      try { await api.login(username, form.elements.password.value); form.elements.password.value = ''; await start(); }
      catch (error) { showLogin(error.status === 401 ? 'Неверный пользователь или пароль.' : error.message, username); }
    } else if (event.target.id === 'server-search-form') {
      event.preventDefault(); return loadList(state.kind, { search: event.target.elements.search.value.trim() });
    } else if (event.target.id === 'server-stock-filter') {
      event.preventDefault(); state.warehouse = event.target.elements.warehouse.value.trim(); return loadStock();
    } else if (event.target.id === 'server-lookup-search') {
      event.preventDefault(); state.lookup.search = event.target.elements.search.value.trim(); state.lookup.start = 0; return loadLookup();
    } else if (event.target.id === 'server-document-form') { event.preventDefault(); return save(); }
  });
  document.addEventListener('input', event => {
    const target = event.target; const path = target.dataset.field;
    if (!path || !state.doc || state.busy || state.unknown) return;
    const previous = getPath(path);
    const value = target.type === 'checkbox' ? (target.checked ? 1 : 0) : target.value;
    setPath(path, value);
    const itemField = /^items\.(\d+)\.(item_code|uom)$/.exec(path);
    if (itemField && previous !== value) {
      const item = state.doc.items[Number(itemField[1])];
      item.conversion_factor = null;
      if (itemField[2] === 'item_code') {
        item.uom = null; item.item_name = '';
        const uom = document.getElementById(`field-items-${itemField[1]}-uom`); if (uom) uom.value = '';
        const hint = target.closest('tr')?.querySelector('.server-item-hint'); if (hint) hint.textContent = '';
      } else if (!value) item.uom = null;
    }
    state.dirty = true;
    const indicator = document.querySelector('.server-save-status'); if (indicator) indicator.innerHTML = '<span class="server-dirty">Изменения не сохранены</span>';
    const actionsBox = document.getElementById('server-actions'); if (actionsBox) actionsBox.innerHTML = actions();
  });
  document.addEventListener('change', event => {
    const target = event.target;
    if (target.id === 'server-company') {
      const company = target.value;
      if (!leaveAllowed()) { target.value = state.company; return; }
      state.company = company; state.search = ''; state.doc = null; state.rows = []; state.dirty = false; ++state.requestId;
      if (state.kind) return loadList(state.kind, { search: '' });
      if (state.view === 'stock') { state.warehouse = ''; return loadStock(); }
      if (state.view === 'exchange') return loadExchange();
      state.view = 'home'; state.error = ''; shell(); setUrl();
    } else if (target.id === 'server-page-size') { state.pageLength = Number(target.value); return loadList(state.kind); }
    else if (target.dataset.field === 'stock_entry_type') { state.doc.purpose = target.value; renderContent(); }
    else if (target.dataset.field === 'party_type') { state.doc.party = ''; state.doc.references = []; renderContent(); }
  });
  dialog.addEventListener('close', () => { const password = dialog.querySelector('input[type=password]'); if (password) password.value = ''; state.lookup = null; });
  window.addEventListener('beforeunload', event => { if (state.dirty || state.busy || state.unknown || state.recoveryIssue) { event.preventDefault(); event.returnValue = ''; } });
  start();
})();
