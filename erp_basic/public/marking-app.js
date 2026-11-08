/* Рабочие места маркировки. Сервер подтверждает локальные операции; внешняя готовность не подменяется сканированием. */
(function (root, factory) {
  const value = factory(typeof module === 'object' && module.exports ? require('./server-api.js') : root.ERPBasicAPI);
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.ERPBasicMarking = value;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (apiTypes) {
  'use strict';
  const { ApiError, isMarkingCommand } = apiTypes;

  const QUEUE_KEY = 'erp-basic:marking-scans:v1';
  const PAGE_LENGTH = 25;
  const MAX_QUEUE = 1000;
  const ROLES = {
    manufacturer: { label: 'Выпуск и маркировка', operations: ['manufacture', 'aggregate', 'dispatch', 'return', 'inventory'], external: 'Заказать коды в СУЗ' },
    wholesale: { label: 'Приёмка и отгрузка', operations: ['receive', 'dispatch', 'return', 'aggregate', 'inventory'], external: 'Передать документ через ЭДО' },
    retail: { label: 'Продажа и возврат', operations: ['sale', 'return', 'receive', 'inventory'], external: 'Оплатить и фискализировать' },
  };
  const OPERATIONS = { manufacture: 'Выпуск', receive: 'Приёмка', dispatch: 'Отгрузка', sale: 'Проверка перед продажей', return: 'Проверка возврата', aggregate: 'Подбор кодов для агрегации', inventory: 'Инвентаризация кодов' };
  const STATUSES = { Draft: 'Подготовка', Open: 'Сканирование', Scanning: 'Сканирование', Prepared: 'Подготовлено локально', Posted: 'Учтено в ERP · ЧЗ не подтверждён', Cancelled: 'Отменено' };
  const RESULTS = { Accepted: 'Принят сервером', Rejected: 'Отклонён', Duplicate: 'Повторный скан', Conflict: 'Конфликт' };
  const HISTORY = { Created: 'Задание создано', QuantityRecorded: 'Сохранено фактическое количество', ExpectedCodesImported: 'Загружен ожидаемый состав', Prepared: 'Локальная подготовка завершена', Cancelled: 'Задание отменено', OperationQueued: 'Создано внешнее задание', ReconciliationRequested: 'Запрошена сверка состояния' };
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const clone = value => JSON.parse(JSON.stringify(value));
  const safeInt = value => Number.isSafeInteger(value) && value >= 0;
  const uuid = () => {
    if (!globalThis.crypto?.randomUUID) throw new ApiError('Для защищённого сканирования требуется HTTPS или localhost.', { code: 'SECURE_CONTEXT_REQUIRED' });
    return globalThis.crypto.randomUUID();
  };
  const validId = value => typeof value === 'string' && /^[a-zA-Z0-9:_-]{8,128}$/.test(value);
  const button = (action, label, options = {}) => `<button type="button" class="btn ${options.primary ? 'primary' : 'outline'}" data-marking-action="${action}" ${options.disabled ? 'disabled' : ''} ${options.extra || ''}>${esc(label)}</button>`;
  const readonlyStatuses = new Set(['Prepared', 'Posted', 'Cancelled', 'Completed', 'Closed']);
  function sessionStorageSafe() { try { return globalThis.sessionStorage || null; } catch (_) { return null; } }

  function validatePage(value, field = 'jobs') {
    if (!value || !Array.isArray(value[field]) || value[field].length > 100 || !safeInt(value.start) || !Number.isSafeInteger(value.page_length) || value.page_length < 1 || value.page_length > 100 || typeof value.has_more !== 'boolean') {
      throw new ApiError('Сервер вернул неверную страницу маркировки. Обновите данные.', { code: 'INVALID_MARKING_PAGE' });
    }
    return value;
  }
  function validateJob(value, company, name = '', uncertain = false) {
    const job = value?.job;
    if (!job || typeof job !== 'object' || Array.isArray(job) || typeof job.name !== 'string' || !job.name || job.company !== company || (name && job.name !== name) || !ROLES[job.role] || !safeInt(job.revision) || !Number.isFinite(job.expected_quantity) || job.expected_quantity < 0 || !safeInt(job.accepted_count) || !safeInt(job.rejected_count) || !Array.isArray(job.lines) || job.lines.length > 100) {
      throw new ApiError('Не удалось подтвердить задание, его версию и организацию в ответе сервера.', { code: 'INVALID_MARKING_JOB', uncertain });
    }
    return job;
  }

  // Это очередь неподтверждённых событий одной вкладки, а не реестр КМ или разрешение на продажу.
  // После ACK исходный код удаляется. Новый пользователь никогда не получает очередь предыдущего.
  class ScanStore {
    constructor(options = {}) {
      this.storage = Object.prototype.hasOwnProperty.call(options, 'storage') ? options.storage : sessionStorageSafe();
      this.createId = options.createId || uuid;
    }
    read(user, companies) {
      if (!this.storage) return null;
      let raw;
      try { raw = this.storage.getItem(QUEUE_KEY); }
      catch (_) { throw new ApiError('Не удалось прочитать очередь сканов этой вкладки.', { code: 'RECOVERY_UNAVAILABLE' }); }
      if (!raw) return null;
      let state;
      try {
        if (raw.length > 5 * 1024 * 1024) throw new Error();
        state = JSON.parse(raw);
        if (state?.version !== 1 || typeof state.user !== 'string' || !validId(state.device_id) || !state.sequences || typeof state.sequences !== 'object' || Array.isArray(state.sequences) || Object.keys(state.sequences).length > 5000 || Object.values(state.sequences).some(value => !safeInt(value))) throw new Error();
      } catch (_) { throw new ApiError('Очередь сканов повреждена. Новые операции заблокированы до проверки заданий на сервере.', { code: 'RECOVERY_CORRUPT', uncertain: true }); }
      if (state.user !== user) { this.clear(); return null; }
      const queue = state.queue;
      if (queue !== null) {
        if (!queue || typeof queue.company !== 'string' || typeof queue.name !== 'string' || !queue.name || !Array.isArray(queue.events) || !queue.events.length || queue.events.length > MAX_QUEUE || queue.events.some(event => !validId(event.event_id) || !Number.isSafeInteger(event.sequence) || event.sequence < 1 || typeof event.raw_code !== 'string' || !event.raw_code.length || event.raw_code.length > 4096) || new Set(queue.events.map(event => event.event_id)).size !== queue.events.length || queue.events.some((event, i) => i && event.sequence !== queue.events[i - 1].sequence + 1) || state.sequences[this.sequenceKey(queue.company, queue.name)] !== queue.events.at(-1).sequence) {
          throw new ApiError('Данные очереди сканирования не прошли проверку. Сверьте задание на сервере перед повторным вводом.', { code: 'RECOVERY_CORRUPT', uncertain: true });
        }
        if (!companies.has(queue.company)) throw new ApiError('Доступ к организации очереди сканирования изменился. Обратитесь к администратору до нового сканирования.', { code: 'RECOVERY_ACCESS_CHANGED', uncertain: true });
      }
      return state;
    }
    sequenceKey(company, name) { return JSON.stringify([company, name]); }
    write(state) {
      if (!this.storage) throw new ApiError('Разрешите хранение данных сеанса: без него скан не может быть надёжно восстановлен.', { code: 'RECOVERY_UNAVAILABLE' });
      try {
        const text = JSON.stringify(state);
        this.storage.setItem(QUEUE_KEY, text);
        if (this.storage.getItem(QUEUE_KEY) !== text) throw new Error();
      } catch (_) { throw new ApiError('Очередь не сохранена. Освободите хранилище сеанса; новый скан пока не отправлен на сервер.', { code: 'SCAN_QUEUE_UNAVAILABLE', uncertain: true }); }
    }
    enqueue(user, companies, company, name, rawCode) {
      if (!companies.has(company)) throw new ApiError('Нет доступа к организации задания.', { code: 'RECOVERY_ACCESS_CHANGED' });
      if (typeof rawCode !== 'string' || !rawCode.length || rawCode.length > 4096) throw new ApiError('Скан должен содержать от 1 до 4096 символов.', { code: 'INVALID_SCAN' });
      const state = this.read(user, companies) || { version: 1, user, device_id: this.createId(), sequences: {}, queue: null };
      if (state.queue && (state.queue.company !== company || state.queue.name !== name)) throw new ApiError('Сначала обработайте очередь предыдущего задания.', { code: 'RECOVERY_REQUIRED' });
      if (!state.queue) state.queue = { company, name, events: [] };
      if (state.queue.events.length >= MAX_QUEUE) throw new ApiError('В очереди 1000 сканов. Дождитесь подтверждения сервера перед следующим сканированием.', { code: 'SCAN_QUEUE_FULL' });
      const key = this.sequenceKey(company, name);
      const sequence = (state.sequences[key] || 0) + 1;
      const event = { event_id: this.createId(), sequence, raw_code: rawCode };
      state.queue.events.push(event); state.sequences[key] = sequence;
      this.write(state);
      return { ...event, device_id: state.device_id };
    }
    acknowledge(user, companies, eventId) {
      const state = this.read(user, companies);
      if (!state?.queue) return;
      const index = state.queue.events.findIndex(event => event.event_id === eventId);
      if (index < 0) return;
      if (index !== 0) throw new ApiError('Сервер подтвердил скан вне очереди. Проверьте задание.', { code: 'SCAN_SEQUENCE_CONFLICT', uncertain: true });
      state.queue.events.shift();
      if (!state.queue.events.length) state.queue = null;
      this.write(state);
    }
    clear() {
      if (!this.storage) return;
      try { this.storage.removeItem(QUEUE_KEY); }
      catch (_) { throw new ApiError('Браузер не позволил удалить очередь сканирования. Очистите данные сеанса перед передачей устройства.', { code: 'RECOVERY_CLEANUP_FAILED', uncertain: true }); }
    }
  }
  function recovery(session, store = new ScanStore()) {
    if (!session?.authenticated) return null;
    const state = store.read(session.user, new Set(session.companies.map(row => row.name)));
    return state?.queue ? { ...state.queue, device_id: state.device_id } : null;
  }

  class Workspace {
    constructor({ api, session, company, pending = null, queued = null, onLock = () => {}, onAuth = () => {} }) {
      this.api = api; this.session = session; this.company = company; this.onLock = onLock; this.onAuth = onAuth;
      this.companies = new Set(session.companies.map(row => row.name)); this.store = new ScanStore();
      this.element = null; this.generation = 0; this.loaded = false;
      this.state = { role: pending?.params.role || 'manufacturer', meta: null, jobs: [], job: null, scans: [], history: [], start: 0, scanStart: 0, hasMore: false, scanHasMore: false, loading: false, busy: false, error: '', notice: '', result: null, pending, queue: queued, paused: Boolean(pending || queued), authIssue: false, recoveryIssue: false, creating: false, cancel: false, sound: false, scanDraft: '', quantityDrafts: {}, importDraft: '', discrepancy: null, discrepancyStale: false, operations: [], form: { profile: '', operation: 'manufacture', warehouse: '', reference_kind: '', reference_name: '', lines: [{ item_code: '', gtin: '', quantity: 1 }] } };
      if (pending) this.state.error = 'Найден незавершённый запрос. Повторите его с прежним ключом, чтобы подтвердить результат.';
      if (queued) this.state.notice = 'Очередь сканов восстановлена. Загрузите актуальную версию задания и продолжите отправку.';
      this.boundClick = event => this.click(event);
      this.boundSubmit = event => this.submit(event);
      this.boundInput = event => this.input(event);
      this.boundKey = event => this.keydown(event);
    }
    mount(element) {
      if (this.element !== element) {
        this.detach(); this.element = element;
        element.addEventListener('click', this.boundClick); element.addEventListener('submit', this.boundSubmit);
        element.addEventListener('input', this.boundInput); element.addEventListener('change', this.boundInput); element.addEventListener('keydown', this.boundKey);
      }
      this.render();
      if (!this.loaded) { this.loaded = true; void this.initialize(); }
    }
    detach() {
      if (this.element) {
        this.element.removeEventListener('click', this.boundClick); this.element.removeEventListener('submit', this.boundSubmit);
        this.element.removeEventListener('input', this.boundInput); this.element.removeEventListener('change', this.boundInput); this.element.removeEventListener('keydown', this.boundKey);
      }
      this.element = null;
    }
    dispose() { ++this.generation; this.detach(); if (this.audio) { void this.audio.close().catch(() => {}); this.audio = null; } }
    suspend(value) { this.state.suspended = value; this.updateRuntime(); }
    refreshSession(session) {
      this.session = session; this.companies = new Set(session.companies.map(row => row.name));
      this.state.authIssue = false;
      this.state.error = this.state.pending ? 'Сессия восстановлена. Повторите сохранённый запрос с прежним ключом.' : '';
    }
    locked() { return Boolean(this.state.suspended || this.state.busy || this.state.pending || this.state.recoveryIssue || this.queueCount()); }
    hasUnconfirmed() { return Boolean(this.state.pending || this.state.recoveryIssue || this.queueCount()); }
    dirty() { return this.state.creating && this.state.form.lines.some(line => line.item_code || line.gtin) || Boolean(this.state.scanDraft) || Object.keys(this.state.quantityDrafts).length > 0 || Boolean(this.state.importDraft); }
    leaveAllowed() {
      if (this.locked()) { this.state.error = 'Сначала подтвердите незавершённый запрос и обработайте очередь сканов.'; this.render(); return false; }
      return !this.dirty() || window.confirm('Есть несохранённый ввод маркировки. Перейти и потерять этот ввод?');
    }
    queueCount() { return this.state.queue?.events?.length || 0; }
    refreshQueue() {
      const stored = this.store.read(this.session.user, this.companies);
      this.state.queue = stored?.queue ? { ...stored.queue, device_id: stored.device_id } : null;
      this.onLock();
    }
    async initialize() {
      const pendingName = this.state.pending?.params.name || this.state.queue?.name;
      await this.loadWorkspace(0, { preserve: true });
      if (pendingName) await this.loadJob(pendingName, 0, { preserve: true });
    }
    async read(method, params) { return this.api.request(method, { company: this.company, ...params }, { namespace: 'marking' }); }
    fail(error) {
      this.state.error = error.message || 'Не удалось выполнить операцию маркировки.';
      this.state.authIssue = error.status === 401 || ['AuthenticationError', 'SessionExpired', 'CSRFTokenError'].includes(error.code);
      if (['RECOVERY_CORRUPT', 'RECOVERY_ACCESS_CHANGED', 'RECOVERY_CLEANUP_FAILED', 'SCAN_QUEUE_UNAVAILABLE'].includes(error.code)) this.state.recoveryIssue = true;
    }
    async loadWorkspace(start = 0, options = {}) {
      const id = ++this.generation; this.state.loading = true;
      if (!options.preserve) { this.state.error = ''; this.state.notice = ''; }
      this.render();
      try {
        const response = validatePage(await this.read('workspace', { role: this.state.role, start, page_length: PAGE_LENGTH }));
        if (id !== this.generation) return;
        if (response.company !== this.company || !Array.isArray(response.roles) || response.roles.some(role => !ROLES[role]) || !Array.isArray(response.profiles) || response.profiles.some(profile => typeof profile.id !== 'string' || !['Preparation', 'Test'].includes(profile.environment) || !['unit', 'quantity'].includes(profile.mode))) throw new ApiError('Сервер вернул неверные настройки рабочего места маркировки.');
        this.state.meta = response; this.state.jobs = response.jobs; this.state.start = response.start; this.state.hasMore = response.has_more;
        if (!response.roles.includes(this.state.role) && response.roles.length) {
          this.state.role = response.roles[0]; this.state.form.operation = ROLES[this.state.role].operations[0];
          this.state.loading = false;
          return this.loadWorkspace(0, options);
        }
        if (!response.profiles.some(profile => profile.id === this.state.form.profile)) this.state.form.profile = response.profiles[0]?.id || '';
      } catch (error) { if (id === this.generation) this.fail(error); }
      finally { if (id === this.generation) { this.state.loading = false; this.render(); } }
    }
    async loadJob(name, start = 0, options = {}) {
      const id = ++this.generation; this.state.loading = true;
      if (this.state.job?.name !== name) { this.state.importDraft = ''; this.state.discrepancy = null; this.state.operations = []; this.state.quantityDrafts = {}; this.state.scanDraft = ''; this.state.job = null; this.state.result = null; this.state.scans = []; this.state.history = []; this.state.permissions = {}; this.state.readiness = null; }
      if (!options.preserve) { this.state.error = ''; this.state.notice = ''; }
      this.render();
      try {
        const result = await this.read('get_job', { name, start, page_length: PAGE_LENGTH });
        if (id !== this.generation) return;
        validatePage(result, 'scans'); validateJob(result, this.company, name);
        const roleChanged = this.state.role !== result.job.role;
        this.accept(result); this.state.scanStart = result.start; this.state.scanHasMore = result.has_more;
        this.state.creating = false;
        if (roleChanged) { this.state.jobs = []; await this.loadWorkspace(0, { preserve: true }); this.focusScan(); }
      } catch (error) { if (id === this.generation) this.fail(error); }
      finally { if (id === this.generation) { this.state.loading = false; this.render(); this.focusScan(); } }
    }
    accept(result) {
      this.state.job = clone(result.job); this.state.role = result.job.role;
      if (Array.isArray(result.scans)) this.state.scans = result.scans.slice(0, PAGE_LENGTH);
      if (typeof result.has_more === 'boolean') this.state.scanHasMore = result.has_more;
      if (safeInt(result.start)) this.state.scanStart = result.start;
      if (Array.isArray(result.history)) this.state.history = result.history.slice(0, 25);
      if (result.permissions) this.state.permissions = result.permissions;
      if (result.readiness) this.state.readiness = result.readiness;
      if (result.discrepancy) { this.state.discrepancy = clone(result.discrepancy); this.state.discrepancyStale = false; }
      if (Array.isArray(result.operations)) this.state.operations = result.operations.slice(0, 25);
      const index = this.state.jobs.findIndex(job => job.name === result.job.name);
      if (index >= 0) this.state.jobs[index] = clone(result.job);
    }
    permissions() { return this.state.job ? this.state.permissions || {} : this.state.meta?.permissions || {}; }
    profileMode() { return this.state.job?.mode || this.state.meta?.profiles?.find(profile => profile.id === this.state.job?.profile)?.mode || null; }
    canScan() { return this.profileMode() === 'unit' && this.permissions().scan === true && this.state.job && !readonlyStatuses.has(this.state.job.status) && (!this.state.busy || this.state.activeMethod === 'marking_scan_code') && !this.state.suspended && !this.state.pending && !this.state.paused && !this.state.recoveryIssue && !this.state.loading && !this.state.importDraft; }
    focusScan() {
      if (!this.element?.isConnected) return;
      const input = this.element.querySelector('#marking-scan');
      if (input && !input.disabled && (!document.activeElement || document.activeElement === document.body || document.activeElement.id === 'marking-scan' || document.activeElement.dataset?.markingAction === 'focus-scan')) input.focus({ preventScroll: true });
    }
    beep(success) {
      if (!this.state.sound) return;
      try {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        if (!AudioContext) return;
        this.audio ||= new AudioContext();
        if (this.audio.state === 'suspended') void this.audio.resume();
        const oscillator = this.audio.createOscillator(); const gain = this.audio.createGain();
        oscillator.frequency.value = success ? 880 : 240; gain.gain.value = 0.04;
        oscillator.connect(gain); gain.connect(this.audio.destination); oscillator.start(); oscillator.stop(this.audio.currentTime + (success ? 0.06 : 0.16));
      } catch (_) { /* Текстовый результат остаётся доступен при отсутствии звука. */ }
    }
    async execute(method, params) {
      if (this.state.busy) return false;
      this.state.busy = true; this.state.activeMethod = method; this.state.error = ''; this.onLock(); this.updateRuntime();
      try {
        const result = await this.api.command(method, params, { validate: value => {
          validateJob(value, this.company, params.name || '', true);
          if (safeInt(params.expected_revision) && value.job.revision < params.expected_revision) throw new ApiError('Сервер вернул устаревшую версию задания. Повторите сохранённый запрос для проверки.', { code: 'INVALID_MARKING_REVISION', uncertain: true });
          if (method === 'marking_scan_code' && (!value.scan || !Object.prototype.hasOwnProperty.call(RESULTS, value.scan.result) || typeof value.scan.name !== 'string')) throw new ApiError('Сервер не подтвердил результат сканирования. Повторите тот же запрос.', { code: 'INVALID_SCAN_RESPONSE', uncertain: true });
        } });
        this.accept(result); this.state.pending = null; this.state.authIssue = false;
        if (method === 'marking_scan_code') {
          this.state.result = result.scan;
          if (this.state.discrepancy?.enabled) this.state.discrepancyStale = true;
          if (this.state.scanStart === 0) {
            const previous = this.state.scans.filter(row => row.name !== result.scan.name);
            if (previous.length >= PAGE_LENGTH) this.state.scanHasMore = true;
            this.state.scans = [result.scan, ...previous].slice(0, PAGE_LENGTH);
          }
          this.store.acknowledge(this.session.user, this.companies, params.event_id); this.refreshQueue();
          this.beep(result.scan.result === 'Accepted');
        } else {
          this.state.creating = false; this.state.cancel = false; this.state.scanStart = 0;
          if (method === 'marking_create_job' && this.state.start === 0 && !this.state.jobs.some(row => row.name === result.job.name)) {
            if (this.state.jobs.length >= PAGE_LENGTH) this.state.hasMore = true;
            this.state.jobs = [clone(result.job), ...this.state.jobs].slice(0, PAGE_LENGTH);
          }
          if (method === 'marking_record_quantity') delete this.state.quantityDrafts[params.gtin];
          if (method === 'marking_import_expected_codes') this.state.importDraft = '';
          this.state.notice = method === 'marking_create_job' ? 'Задание сохранено на сервере. Можно начинать проверку.' : method === 'marking_cancel_job' ? 'Сервер подтвердил отмену локального задания.' : method === 'marking_record_quantity' ? 'Фактическое количество сохранено на сервере. Регуляторный формат определяется профилем строки.' : method === 'marking_import_expected_codes' ? 'Ожидаемый состав сохранён отдельно от фактических сканов. Сверка покажет недостающие и лишние экземпляры.' : 'Локальная подготовка завершена. Передача в ЧЗ, ЭДО и кассу этим действием не выполнялась.';
        }
        return true;
      } catch (error) {
        this.fail(error);
        if (error.uncertain && !this.state.recoveryIssue) this.state.pending = { method, params: clone(params) };
        if (method === 'marking_scan_code') this.state.paused = true;
        this.beep(false); return false;
      } finally {
        this.state.busy = false; this.state.activeMethod = ''; this.onLock();
        if (method === 'marking_scan_code' && this.state.job && !this.state.creating) this.updateRuntime(); else this.render();
        this.focusScan();
      }
    }
    enqueue(rawCode) {
      if (!this.canScan()) return;
      try {
        this.store.enqueue(this.session.user, this.companies, this.company, this.state.job.name, rawCode);
        this.refreshQueue(); this.state.scanDraft = '';
        const input = this.element?.querySelector('#marking-scan'); if (input) input.value = '';
        this.updateRuntime(); this.focusScan(); void this.drain();
      } catch (error) { this.fail(error); this.updateRuntime(); }
    }
    async drain() {
      if (this.draining || this.state.paused || this.state.pending || this.state.recoveryIssue || !this.state.job) return;
      this.draining = true;
      try {
        while (this.queueCount() && !this.state.paused && !this.state.pending && !this.state.recoveryIssue) {
          const event = this.state.queue.events[0];
          const ok = await this.execute('marking_scan_code', { company: this.company, name: this.state.job.name, raw_code: event.raw_code, expected_revision: this.state.job.revision, device_id: this.state.queue.device_id, event_id: event.event_id, sequence: event.sequence });
          if (!ok) break;
        }
      } finally { this.draining = false; this.updateRuntime(); this.onLock(); }
    }
    async retry() {
      if (!this.state.pending || this.state.busy || this.state.recoveryIssue) return;
      const pending = clone(this.state.pending);
      if (await this.execute(pending.method, pending.params)) { this.state.paused = false; this.updateRuntime(); await this.drain(); this.focusScan(); }
    }
    async resume() {
      if (this.state.pending || this.state.busy || this.state.recoveryIssue || !this.state.queue) return;
      this.state.paused = true;
      await this.loadJob(this.state.queue.name, 0, { preserve: false });
      if (this.state.error || !this.permissions().scan || readonlyStatuses.has(this.state.job?.status)) {
        this.state.error ||= 'Задание закрыто или право сканирования отозвано. Очередь сохранена, обратитесь к администратору.'; this.render(); return;
      }
      this.state.paused = false; this.state.notice = 'Отправляем сохранённые события. Каждый скан подтверждает сервер.'; this.render(); this.focusScan(); await this.drain();
    }
    readinessHtml() {
      const ready = this.state.readiness || this.state.meta?.readiness;
      const blockers = Array.isArray(ready?.blockers) ? ready.blockers.slice(0, 30) : [];
      return `<section class="marking-readiness" aria-label="Готовность интеграции"><div><strong>Внешние операции недоступны</strong><p>Сканирование подтверждает локальную проверку. Готовность ЧЗ, ЭДО и кассы проверяется отдельно.</p></div>${blockers.length ? `<ul>${blockers.map(item => `<li>${esc(item.message || item.code)}</li>`).join('')}</ul>` : '<p>Требуются утверждённый профиль товарной группы и проверенные адаптеры внешнего контура.</p>'}${button('readiness', 'Проверить готовность', { disabled: this.locked() || this.state.loading })}${button('external', ROLES[this.state.role]?.external || 'Выполнить внешнюю операцию', { disabled: true })}</section>`;
    }
    messagesHtml() {
      const state = this.state;
      if (state.pending) return `<div class="server-notice server-unknown" role="alert"><strong>Результат операции пока не подтверждён.</strong><p>${esc(state.error)}</p><p>Организация: ${esc(this.company)}. Повтор отправит прежние данные и прежний ключ. Новые операции заблокированы.</p>${button('retry', 'Повторить тот же запрос', { disabled: state.busy || state.recoveryIssue })}${state.authIssue ? button('reauth', 'Войти повторно') : ''}</div>`;
      return `${state.error ? `<div class="server-error" role="alert"><p>${esc(state.error)}</p>${state.authIssue ? button('reauth', 'Войти повторно') : ''}${state.recoveryIssue ? '<p>Очередь сохранена для проверки; новые операции заблокированы.</p>' : ''}</div>` : ''}${state.notice ? `<div class="server-notice" role="status">${esc(state.notice)}</div>` : ''}`;
    }
    environmentHtml(job) {
      return job.environment === 'Test' ? '<span class="marking-environment marking-test">Учебные данные · TEST</span>' : '<span class="marking-environment">Подготовка · внешние операции закрыты</span>';
    }
    render() {
      if (!this.element?.isConnected) return;
      const focus = this.element.contains(document.activeElement) ? document.activeElement.id : '';
      const state = this.state; const roles = state.meta?.roles || Object.keys(ROLES);
      this.element.innerHTML = `<section class="marking-workspace" aria-label="Маркировка"><div class="marking-heading"><div><p class="marking-eyebrow">Честный знак · локальная подготовка</p><h1>Маркировка</h1><p>Одна организация, одно задание, проверяемый результат каждого скана.</p></div><span class="marking-company">${esc(this.company)}</span></div><div class="marking-role-tabs" role="group" aria-label="Рабочее место">${roles.map(role => button('role', ROLES[role].label, { primary: role === state.role, disabled: this.locked() || state.loading, extra: `data-role="${esc(role)}" aria-pressed="${role === state.role}"` })).join('')}</div><div id="marking-messages">${this.messagesHtml()}</div>${state.loading ? '<p class="server-loading" role="status">Загружаем данные маркировки…</p>' : ''}${this.readinessHtml()}${state.meta ? `<div class="marking-columns"><aside class="marking-jobs" aria-label="Задания"><div class="marking-section-heading"><h2>Задания</h2>${state.meta.permissions?.create ? button('new', 'Новое', { disabled: this.locked() || state.loading }) : ''}</div>${this.jobsHtml()}</aside><div class="marking-detail">${state.creating ? this.createHtml() : state.job ? this.jobHtml() : `<div class="marking-empty"><h2>${state.pending ? 'Восстановление запроса' : 'Выберите задание или создайте новое'}</h2><p>${state.pending ? 'Повторите сохранённую операцию с прежним ключом. Сервер вернёт её результат.' : 'Здесь будут товарные строки, сканирование, расхождения и история. Учётные остатки меняются штатными документами ERPNext.'}</p></div>`}</div></div>` : state.loading ? '' : `<div class="marking-empty"><p>Настройки маркировки не загружены. Локальные данные не подставляются.</p>${button('readiness', 'Повторить загрузку', { disabled: this.locked() })}</div>`}</section>`;
      if (focus) this.element.querySelector(`#${CSS.escape(focus)}`)?.focus({ preventScroll: true });
      this.onLock();
    }
    jobsHtml() {
      const state = this.state;
      return `${state.jobs.length ? `<ul class="marking-job-list">${state.jobs.map(job => `<li><button type="button" class="marking-job ${state.job?.name === job.name ? 'is-active' : ''}" data-marking-action="job" data-name="${esc(job.name)}" ${this.locked() || state.loading ? 'disabled' : ''}><strong>${esc(job.name)}</strong><span>${esc(OPERATIONS[job.operation] || job.operation)} · ${esc(STATUSES[job.status] || job.status)}</span><small>${esc(job.accepted_count ?? 0)} / ${esc(job.expected_quantity ?? 0)} · ${esc(job.environment === 'Test' ? 'Учебное' : 'Подготовка')}</small></button></li>`).join('')}</ul>` : '<p class="server-empty">В этом рабочем месте пока нет заданий.</p>'}<div class="marking-pagination"><span>${esc(state.jobs.length ? `${state.start + 1}–${state.start + state.jobs.length}` : '0')}</span>${button('jobs-prev', 'Назад', { disabled: !state.start || this.locked() || state.loading })}${button('jobs-next', 'Далее', { disabled: !state.hasMore || this.locked() || state.loading })}</div>`;
    }
    createHtml() {
      const form = this.state.form; const profiles = this.state.meta?.profiles || [];
      return `<form id="marking-create-form" class="marking-create"><h2>Новое задание</h2><p class="server-field-hint">Локальная подготовка кодов. Создание задания не выпускает продукцию и не проводит продажу.</p><fieldset ${this.locked() || this.state.loading ? 'disabled' : ''}><div class="marking-form-grid"><div class="field"><label for="marking-operation">Операция</label><select id="marking-operation" data-marking-field="operation">${ROLES[this.state.role].operations.map(operation => `<option value="${operation}" ${form.operation === operation ? 'selected' : ''}>${esc(OPERATIONS[operation])}</option>`).join('')}</select></div><div class="field"><label for="marking-profile">Профиль правил</label><select id="marking-profile" data-marking-field="profile" required><option value="">Выберите профиль</option>${profiles.map(profile => `<option value="${esc(profile.id)}" ${form.profile === profile.id ? 'selected' : ''}>${esc(profile.label)}${profile.environment === 'Test' ? ' · УЧЕБНЫЙ' : ' · подготовка'}</option>`).join('')}</select></div><div class="field"><label for="marking-warehouse">Склад</label><input id="marking-warehouse" data-marking-field="warehouse" value="${esc(form.warehouse)}" maxlength="140" placeholder="Точное имя склада ERPNext"></div><div class="field"><label for="marking-reference-kind">Документ-основание</label><select id="marking-reference-kind" data-marking-field="reference_kind"><option value="">Без связи с учётным документом</option>${[['stockDocs', 'Складской документ / выпуск'], ['receipts', 'Приёмка'], ['deliveries', 'Отгрузка'], ['invoices', 'Счёт покупателю']].map(([kind, label]) => `<option value="${kind}" ${form.reference_kind === kind ? 'selected' : ''}>${label}</option>`).join('')}</select></div><div class="field"><label for="marking-reference-name">Номер документа</label><input id="marking-reference-name" data-marking-field="reference_name" value="${esc(form.reference_name)}" maxlength="140" ${form.reference_kind ? 'required' : ''} placeholder="Например MAT-STE-2026-00001"></div></div>${form.reference_kind ? `<div class="marking-source-load">${button('load-reference', 'Загрузить строки основания')}<p class="server-field-hint">Заменит текущие товарные строки данными сервера. GTIN укажите явно.</p></div>` : ''}<h3>Товарные строки</h3><div class="marking-lines-form">${form.lines.map((line, index) => `<div class="marking-form-line"><span class="marking-line-number">${index + 1}</span><div class="field"><label for="marking-item-${index}">Товар ERPNext</label><input id="marking-item-${index}" data-marking-line="${index}" data-marking-cell="item_code" value="${esc(line.item_code)}" maxlength="140" required ${form.reference_kind && line.reference_line ? 'readonly' : ''}>${form.reference_kind && line.reference_line ? `<small class="server-field-hint">Строка основания: ${esc(line.reference_line)}</small>` : ''}</div><div class="field"><label for="marking-gtin-${index}">GTIN, 14 цифр</label><input id="marking-gtin-${index}" data-marking-line="${index}" data-marking-cell="gtin" value="${esc(line.gtin)}" inputmode="numeric" pattern="[0-9]{14}" minlength="14" maxlength="14" required></div><div class="field"><label for="marking-quantity-${index}">Количество</label><input id="marking-quantity-${index}" type="number" data-marking-line="${index}" data-marking-cell="quantity" value="${esc(line.quantity)}" min="1" max="100000" step="1" required ${form.reference_kind && line.reference_line ? 'readonly' : ''}></div>${button('remove-line', 'Убрать', { disabled: form.lines.length <= 1, extra: `data-index="${index}" aria-label="Убрать строку ${index + 1}"` })}</div>`).join('')}</div><div class="marking-form-actions">${button('add-line', '+ Строка', { disabled: form.lines.length >= 100 || Boolean(form.reference_kind) })}<button type="submit" class="btn primary" ${!profiles.length ? 'disabled' : ''}>Сохранить задание</button>${button('close-form', 'Закрыть')}</div></fieldset></form>`;
    }
    jobHtml() {
      const job = this.state.job;
      return `<article class="marking-job-detail"><div class="marking-section-heading"><div><h2>${esc(job.name)}</h2>${this.environmentHtml(job)}</div>${button('reload-job', 'Обновить задание', { disabled: this.locked() || this.state.loading })}</div><dl class="marking-facts"><div><dt>Операция</dt><dd>${esc(OPERATIONS[job.operation] || job.operation)}</dd></div><div><dt>Этап</dt><dd id="marking-job-status">${esc(STATUSES[job.status] || job.status)}</dd></div><div><dt>Организация / склад</dt><dd>${esc(this.company)}<small>${esc(job.warehouse || 'Склад не указан')}</small></dd></div><div><dt>Основание</dt><dd>${job.reference_name ? `${esc(job.reference_doctype || '')} · ${esc(job.reference_name)}` : '<span class="marking-warning">Не связано с учётным документом</span>'}</dd></div></dl>${job.role === 'retail' ? '<div class="marking-retail-note">Это проверка набора кодов. Оплата, фискализация и возврат денег здесь не выполняются; успешный скан не означает продажу.</div>' : ''}<div id="marking-runtime">${this.runtimeHtml()}</div><div class="table-scroller"><table class="marking-lines-table"><caption>Строки задания</caption><thead><tr><th>Товар</th><th>GTIN</th><th>Ожидается</th><th>Принято сервером</th></tr></thead><tbody id="marking-lines-body">${this.linesHtml()}</tbody></table></div><div id="marking-discrepancy">${this.discrepancyHtml()}</div><section class="marking-scans"><div class="marking-section-heading"><h3>Сканы и расхождения</h3><span class="server-field-hint">Полные КМ в журнале не показываются</span></div><div id="marking-scans-table">${this.scansHtml()}</div></section><section class="marking-history"><h3>Последние события задания</h3>${this.state.history.length ? `<ol>${this.state.history.map(event => `<li><strong>${esc(HISTORY[event.action] || event.action || event.event_type || event.operation || 'Изменение')}</strong><span>${esc(event.message || event.reason || event.status || '')}</span><small>${esc(event.actor || event.user || '')} · ${esc(event.creation || event.created_at || '')}</small></li>`).join('')}</ol>` : '<p class="server-field-hint">История загружается с сервера при открытии или обновлении задания.</p>'}<p class="server-field-hint">Показано до 25 последних событий. Внешнее состояние: не подтверждено. Время внешней проверки отсутствует.</p></section>${this.operationsHtml()}</article>`;
    }
    linesHtml() { return this.state.job.lines.map(line => `<tr><td>${esc(line.item_code)}</td><td><code>${esc(line.gtin)}</code></td><td>${esc(line.quantity)}</td><td>${esc(line.accepted_quantity ?? 0)}</td></tr>`).join(''); }
    quantityHtml() {
      const job = this.state.job;
      const disabled = this.locked() || this.state.loading || !this.permissions().scan || readonlyStatuses.has(job.status);
      return `<section class="marking-quantity"><h3>Фактическое количество · ОСУ</h3><p class="server-field-hint">Количество сверяется по GTIN. Скан каждого КМ для этого профиля не требуется и не меняет регуляторный режим учёта.</p>${job.lines.map((line, index) => `<form class="marking-quantity-form" data-gtin="${esc(line.gtin)}"><label for="marking-actual-${index}">${esc(line.item_code)} · ${esc(line.gtin)}</label><input id="marking-actual-${index}" name="quantity" type="number" value="${esc(this.state.quantityDrafts[line.gtin] ?? line.accepted_quantity ?? 0)}" min="0" max="1000000" step="1" required ${disabled ? 'disabled' : ''}><button type="submit" class="btn outline" ${disabled ? 'disabled' : ''}>Сохранить количество</button></form>`).join('')}</section>`;
    }
    discrepancyHtml() {
      if (this.profileMode() === 'quantity') return '';
      const job = this.state.job; const snapshot = this.state.discrepancy;
      const canImport = this.permissions().import && !readonlyStatuses.has(job.status) && job.accepted_count === 0 && job.rejected_count === 0;
      const disabled = this.locked() || this.state.loading;
      const missing = Array.isArray(snapshot?.missing) ? snapshot.missing.slice(0, PAGE_LENGTH) : [];
      return `<section class="marking-discrepancy"><div class="marking-section-heading"><h3>Сверка с ожидаемым составом</h3>${snapshot?.enabled ? button('discrepancy-refresh', 'Обновить сверку', { disabled }) : ''}</div>${snapshot?.enabled ? `<p class="${this.state.discrepancyStale ? 'marking-warning' : 'server-field-hint'}">${this.state.discrepancyStale ? 'Снимок сверки устарел после новых сканов. Обновите его для актуального результата.' : 'Снимок сверки получен с сервера.'}</p><div class="marking-discrepancy-counts"><span>Недостающих: <strong>${esc(snapshot.missing_count)}</strong></span><span>Лишних принятых: <strong>${esc(snapshot.extra_count)}</strong></span></div>${missing.length ? `<div class="table-scroller"><table><thead><tr><th>Недостающий экземпляр</th><th>GTIN</th></tr></thead><tbody>${missing.map(code => `<tr><td><code>${esc(code.code_masked)}</code></td><td>${esc(code.gtin)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="server-field-hint">На этой странице недостающих экземпляров нет.</p>'}<div class="marking-pagination"><span>${esc(missing.length ? `${Number(snapshot.start || 0) + 1}–${Number(snapshot.start || 0) + missing.length}` : '0')}</span>${button('discrepancy-prev', 'Предыдущие расхождения', { disabled: disabled || !snapshot.start })}${button('discrepancy-next', 'Следующие расхождения', { disabled: disabled || !snapshot.has_more })}</div>` : '<p class="server-field-hint">Ожидаемый состав поставщика не загружен. Пока проверяются GTIN и количество; фактические сканы не создают состав поставщика.</p>'}${canImport ? `<details class="marking-import" ${this.state.importDraft ? 'open' : ''}><summary>Загрузить ожидаемые КИ</summary><form id="marking-import-form"><label for="marking-expected-codes">КИ без криптохвоста: один код на строку, до 1000 за один запрос</label><textarea id="marking-expected-codes" name="codes" rows="5" maxlength="300000" spellcheck="false" autocapitalize="off" autocomplete="off" required ${disabled ? 'disabled' : ''}>${esc(this.state.importDraft)}</textarea><p class="server-field-hint">Загружайте согласованный состав до первого скана. Сервер проверит формат и GTIN. Исходный список удаляется из этой вкладки после подтверждения записи.</p><button type="submit" class="btn outline" ${disabled ? 'disabled' : ''}>Сохранить ожидаемый состав</button></form></details>` : ''}</section>`;
    }
    operationsHtml() {
      return `<section class="marking-external-history"><h3>Внешние операции</h3><p class="server-field-hint">Тестовый исход, внешней передачи нет. Этот журнал не подтверждает обработку в ГИС МТ, ЭДО или на кассе.</p>${this.state.operations.length ? `<div class="table-scroller"><table><thead><tr><th>Операция</th><th>Состояние теста</th><th>Контур</th><th>Причина</th></tr></thead><tbody>${this.state.operations.map(operation => `<tr><td>${esc(operation.operation)}</td><td>${esc(operation.state)}</td><td>${esc(operation.environment)} · тест</td><td>${esc(operation.reason || '')}</td></tr>`).join('')}</tbody></table></div>` : '<p class="server-field-hint">Внешние задания не создавались.</p>'}</section>`;
    }
    async loadDiscrepancy(start = 0) {
      if (!this.state.job || this.locked()) return;
      const name = this.state.job.name; const id = ++this.generation; this.state.loading = true; this.state.error = ''; this.render();
      try {
        const response = validatePage(await this.read('get_discrepancy', { name, start, page_length: PAGE_LENGTH }), 'missing');
        if (id !== this.generation) return;
        if (!safeInt(response.missing_count) || !safeInt(response.extra_count) || typeof response.enabled !== 'boolean') throw new ApiError('Сервер вернул неверный результат сверки.');
        this.state.discrepancy = response; this.state.discrepancyStale = false;
      } catch (error) { if (id === this.generation) this.fail(error); }
      finally { if (id === this.generation) { this.state.loading = false; this.render(); } }
    }
    async loadReference() {
      const form = this.state.form;
      if (this.locked() || !form.reference_kind || !form.reference_name.trim()) { this.state.error = 'Сначала выберите тип и номер документа-основания.'; this.render(); return; }
      const kind = form.reference_kind; const name = form.reference_name.trim();
      const id = ++this.generation; this.state.loading = true; this.state.error = ''; this.render();
      try {
        const response = await this.api.request('get_document', { company: this.company, kind, name });
        if (id !== this.generation) return;
        const doc = response?.document;
        if (response?.company !== this.company || response?.kind !== kind || !doc || doc.name !== name || !Array.isArray(doc.items) || !doc.items.length || doc.items.length > 100 || Number(doc.docstatus) === 2) throw new ApiError('Не удалось подтвердить доступное основание с 1–100 товарными строками.');
        const lines = doc.items.map(row => {
          const quantity = Math.abs(Number(row.stock_qty ?? row.qty));
          if (typeof row.name !== 'string' || !row.name || typeof row.item_code !== 'string' || !Number.isSafeInteger(quantity) || quantity < 1 || quantity > 100000) throw new ApiError('Основание содержит строку без товара или без целого количества складских единиц. Для этого профиля нужен отдельный подходящий документ.');
          return { item_code: row.item_code, gtin: '', quantity, reference_line: row.name };
        });
        this.state.form.lines = lines;
        this.state.referenceLoaded = { kind, name };
        this.state.notice = 'Строки основания загружены с сервера. Укажите GTIN для каждой выбранной строки; ненужные строки можно убрать.';
      } catch (error) { if (id === this.generation) this.fail(error); }
      finally { if (id === this.generation) { this.state.loading = false; this.render(); this.element?.querySelector('#marking-gtin-0')?.focus(); } }
    }
    runtimeHtml() {
      const state = this.state; const job = state.job;
      const readonly = readonlyStatuses.has(job.status); const count = this.queueCount();
      const result = state.result;
      return `<div class="marking-counters" aria-label="Счётчики задания"><div><strong>${esc(job.expected_quantity)}</strong><span>Ожидается</span></div><div><strong>${esc(job.accepted_count)}</strong><span>Принято сервером</span></div><div><strong>${esc(job.rejected_count)}</strong><span>Проблемных сканов</span></div><div><strong>${esc(count)}</strong><span>Ждут сервера</span></div></div><progress class="marking-progress" max="${esc(Math.max(job.expected_quantity, 1))}" value="${esc(Math.min(job.accepted_count, job.expected_quantity))}" aria-label="Доля принятых сервером единиц"></progress>${!readonly && this.profileMode() !== 'quantity' ? `<form id="marking-scan-form" class="marking-scan-form"><label for="marking-scan">Сканируйте код и нажмите Enter</label><div class="marking-scan-input-row"><input id="marking-scan" name="raw_code" value="${esc(state.scanDraft)}" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="4096" aria-describedby="marking-scan-help" ${this.canScan() ? '' : 'disabled'}><button type="submit" class="btn primary" ${this.canScan() ? '' : 'disabled'}>Принять скан</button></div><p id="marking-scan-help">Разделители GS сохраняются. Enter отправляет полный скан; F2 возвращает фокус. Проверку прав, товара и дублей выполняет сервер.</p></form><div class="marking-scan-options">${button('focus-scan', 'Фокус сканера · F2', { disabled: !this.canScan() })}<label><input id="marking-sound" type="checkbox" ${state.sound ? 'checked' : ''}> Звук результата</label></div>` : this.profileMode() === 'quantity' ? this.quantityHtml() : ''}<div class="marking-result ${result && result.result !== 'Accepted' ? 'marking-result-error' : ''}" role="status" aria-live="polite" aria-atomic="true">${result ? `<strong>${esc(RESULTS[result.result] || result.result)}</strong><span>${esc(result.reason || '')}</span><small>${esc(result.code_masked || '')}</small>` : `<strong>${this.profileMode() === 'quantity' ? 'Ожидаем сверку количества' : 'Ожидаем сканирование'}</strong><span>Каждый результат появится после устойчивой записи на сервере.</span>`}</div>${count ? `<div class="marking-queue-note" role="status">${esc(count)} событий сохранены в очереди этой вкладки и ещё не подтверждены сервером.${state.paused && !state.pending ? button('resume', 'Загрузить версию и продолжить очередь', { disabled: state.busy || state.recoveryIssue || state.loading }) : ''}</div>` : ''}<div class="marking-local-actions">${this.permissions().complete && !readonly ? button('complete', 'Завершить локальную подготовку', { disabled: this.locked() || state.loading || !job.lines.every(line => Number(line.accepted_quantity || 0) === Number(line.quantity)), primary: true }) : ''}${this.permissions().complete && !readonly ? button('cancel', 'Отменить задание', { disabled: this.locked() || state.loading }) : ''}</div>${state.cancel ? `<form id="marking-cancel-form" class="marking-cancel-form"><label for="marking-cancel-reason">Причина отмены</label><input id="marking-cancel-reason" name="reason" maxlength="500" required><button type="submit" class="btn outline" ${this.locked() ? 'disabled' : ''}>Подтвердить отмену</button></form>` : ''}<p class="server-field-hint">${readonly ? 'Задание доступно для просмотра. Регуляторное подтверждение этим статусом не устанавливается.' : job.accepted_count < job.expected_quantity ? `Следующее действие: подтвердить на сервере ещё ${esc(job.expected_quantity - job.accepted_count)} единиц и проверить расхождения.` : 'Следующее действие: проверьте расхождения и завершите локальную подготовку.'}</p>`;
    }
    scansHtml() {
      const state = this.state;
      return `${state.scans.length ? `<div class="table-scroller"><table><thead><tr><th>Результат</th><th>Код</th><th>Причина</th><th>Время сервера</th></tr></thead><tbody>${state.scans.map(scan => `<tr><td><span class="marking-scan-badge ${scan.result === 'Accepted' ? 'is-accepted' : 'is-problem'}">${esc(RESULTS[scan.result] || scan.result)}</span></td><td><code>${esc(scan.code_masked || '')}</code></td><td>${esc(scan.reason || '')}</td><td>${esc(scan.creation || '')}</td></tr>`).join('')}</tbody></table></div>` : '<p class="server-field-hint">Подтверждённых сканов пока нет.</p>'}<div class="marking-pagination"><span>${esc(state.scans.length ? `${state.scanStart + 1}–${state.scanStart + state.scans.length}` : '0')}</span>${button('scans-prev', 'Предыдущие сканы', { disabled: !state.scanStart || this.locked() || state.loading })}${button('scans-next', 'Следующие сканы', { disabled: !state.scanHasMore || this.locked() || state.loading })}</div>`;
    }
    updateRuntime() {
      if (!this.element?.isConnected || !this.state.job) { this.render(); return; }
      const input = this.element.querySelector('#marking-scan');
      const focused = document.activeElement === input;
      // Не заменяем поле во время непрерывного ввода: новый скан может уже поступать, пока сервер отвечает на предыдущий.
      if (input) this.state.scanDraft = input.value;
      const runtime = this.element.querySelector('#marking-runtime');
      if (runtime) {
        const holder = document.createElement('div'); holder.innerHTML = this.runtimeHtml();
        const nextInput = holder.querySelector('#marking-scan');
        if (input && nextInput) { input.disabled = nextInput.disabled; nextInput.replaceWith(input); }
        runtime.replaceChildren(...holder.childNodes);
      }
      const messages = this.element.querySelector('#marking-messages'); if (messages) messages.innerHTML = this.messagesHtml();
      const lines = this.element.querySelector('#marking-lines-body'); if (lines) lines.innerHTML = this.linesHtml();
      const scans = this.element.querySelector('#marking-scans-table'); if (scans) scans.innerHTML = this.scansHtml();
      const discrepancy = this.element.querySelector('#marking-discrepancy'); if (discrepancy && !this.state.importDraft) discrepancy.innerHTML = this.discrepancyHtml();
      this.element.querySelectorAll('#marking-import-form textarea,#marking-import-form button').forEach(node => { node.disabled = this.locked() || this.state.loading; });
      const status = this.element.querySelector('#marking-job-status'); if (status) status.textContent = STATUSES[this.state.job.status] || this.state.job.status;
      this.element.querySelectorAll('[data-marking-action="role"],[data-marking-action="new"],[data-marking-action="job"],[data-marking-action="readiness"],[data-marking-action="reload-job"]').forEach(node => { node.disabled = this.locked() || this.state.loading; });
      if (focused && input && !input.disabled) input.focus({ preventScroll: true });
      this.onLock();
    }
    async click(event) {
      const target = event.target.closest('[data-marking-action]'); if (!target || !this.element?.contains(target) || target.disabled) return;
      event.preventDefault();
      const action = target.dataset.markingAction;
      if (action === 'reauth') return this.onAuth();
      if (action === 'retry') return this.retry();
      if (action === 'resume') return this.resume();
      if (action === 'focus-scan') { this.element.querySelector('#marking-scan')?.focus(); return; }
      if (this.locked()) return;
      if (action === 'load-reference') return this.loadReference();
      if (action === 'discrepancy-refresh') return this.loadDiscrepancy();
      if (action === 'discrepancy-prev' || action === 'discrepancy-next') return this.loadDiscrepancy(Math.max(0, Number(this.state.discrepancy?.start || 0) + (action === 'discrepancy-prev' ? -PAGE_LENGTH : PAGE_LENGTH)));
      if (action === 'readiness') { await this.loadWorkspace(this.state.start); if (this.state.job) await this.loadJob(this.state.job.name, this.state.scanStart); return; }
      if (action === 'role') {
        if (!ROLES[target.dataset.role] || !this.leaveAllowed()) return;
        this.state.role = target.dataset.role; this.state.importDraft = ''; this.state.discrepancy = null; this.state.operations = []; this.state.quantityDrafts = {}; this.state.job = null; this.state.result = null; this.state.readiness = null; this.state.creating = false; this.state.scanDraft = ''; this.state.form.operation = ROLES[this.state.role].operations[0];
        return this.loadWorkspace();
      }
      if (action === 'new') {
        if (!this.leaveAllowed()) return;
        this.state.creating = true; this.state.importDraft = ''; this.state.discrepancy = null; this.state.operations = []; this.state.quantityDrafts = {}; this.state.job = null; this.state.result = null; this.state.readiness = null; this.state.error = ''; this.state.notice = ''; this.state.scanDraft = '';
        this.state.form = { profile: this.state.form.profile, operation: ROLES[this.state.role].operations[0], warehouse: '', reference_kind: '', reference_name: '', lines: [{ item_code: '', gtin: '', quantity: 1 }] };
        this.render(); this.element.querySelector('#marking-item-0')?.focus(); return;
      }
      if (action === 'close-form') { if (this.leaveAllowed()) { this.state.creating = false; this.render(); } return; }
      if (action === 'add-line') { if (this.state.form.lines.length < 100) this.state.form.lines.push({ item_code: '', gtin: '', quantity: 1 }); this.render(); this.element.querySelector(`#marking-item-${this.state.form.lines.length - 1}`)?.focus(); return; }
      if (action === 'remove-line') { const index = Number(target.dataset.index); if (this.state.form.lines.length > 1 && safeInt(index)) this.state.form.lines.splice(index, 1); this.render(); return; }
      if (action === 'job') { if (this.leaveAllowed()) return this.loadJob(target.dataset.name); return; }
      if (action === 'reload-job') return this.loadJob(this.state.job.name);
      if (action === 'jobs-prev' || action === 'jobs-next') return this.loadWorkspace(Math.max(0, this.state.start + (action === 'jobs-prev' ? -PAGE_LENGTH : PAGE_LENGTH)));
      if (action === 'scans-prev' || action === 'scans-next') return this.loadJob(this.state.job.name, Math.max(0, this.state.scanStart + (action === 'scans-prev' ? -PAGE_LENGTH : PAGE_LENGTH)));
      if (['complete', 'cancel'].includes(action) && (this.state.scanDraft || this.state.importDraft || Object.keys(this.state.quantityDrafts).length)) { this.state.error = 'Есть неотправленный код или несохранённое фактическое количество. Сохраните ввод или очистите поле перед завершением задания.'; this.updateRuntime(); return; }
      if (action === 'complete') return this.execute('marking_complete_job', { company: this.company, name: this.state.job.name, expected_revision: this.state.job.revision });
      if (action === 'cancel') { this.state.cancel = !this.state.cancel; this.updateRuntime(); this.element.querySelector('#marking-cancel-reason')?.focus(); }
    }
    async submit(event) {
      if (!['marking-scan-form', 'marking-create-form', 'marking-cancel-form', 'marking-import-form'].includes(event.target.id) && !event.target.classList.contains('marking-quantity-form')) return;
      event.preventDefault();
      if (event.target.id === 'marking-scan-form') { const input = event.target.elements.raw_code; if (input.value) this.enqueue(input.value); return; }
      if (this.locked() || !event.target.reportValidity()) return;
      if (event.target.id === 'marking-import-form') {
        const codes = event.target.elements.codes.value.split(/\r\n|\n|\r/);
        if (codes.at(-1) === '') codes.pop();
        if (!codes.length || codes.length > 1000 || codes.some(code => !code.length) || JSON.stringify(codes).length > 300000) { this.state.error = 'Загрузите от 1 до 1000 КИ без пустых строк, объёмом до 300 КБ. Каждый код сохранится без исправления символов.'; this.render(); return; }
        return this.execute('marking_import_expected_codes', { company: this.company, name: this.state.job.name, codes, expected_revision: this.state.job.revision });
      }
      if (event.target.classList.contains('marking-quantity-form')) return this.execute('marking_record_quantity', { company: this.company, name: this.state.job.name, gtin: event.target.dataset.gtin, quantity: Number(event.target.elements.quantity.value), expected_revision: this.state.job.revision });
      if (event.target.id === 'marking-cancel-form') return this.execute('marking_cancel_job', { company: this.company, name: this.state.job.name, expected_revision: this.state.job.revision, reason: event.target.elements.reason.value.trim() });
      const form = this.state.form;
      if (form.reference_kind && (this.state.referenceLoaded?.kind !== form.reference_kind || this.state.referenceLoaded?.name !== form.reference_name.trim() || form.lines.some(line => !line.reference_line))) { this.state.error = 'Загрузите строки выбранного основания перед сохранением связанного задания.'; this.render(); return; }
      const params = { company: this.company, role: this.state.role, profile: form.profile, operation: form.operation, lines: form.lines.map(line => ({ item_code: line.item_code.trim(), gtin: line.gtin, quantity: Number(line.quantity), ...(form.reference_kind ? { reference_line: line.reference_line } : {}) })) };
      if (form.warehouse.trim()) params.warehouse = form.warehouse.trim();
      if (form.reference_kind) { params.reference_kind = form.reference_kind; params.reference_name = form.reference_name.trim(); }
      if (await this.execute('marking_create_job', params)) { this.render(); this.element.querySelector('#marking-scan')?.focus(); }
    }
    input(event) {
      const target = event.target;
      if (target.id === 'marking-scan') { this.state.scanDraft = target.value; return; }
      if (target.id === 'marking-expected-codes') {
        if (this.locked()) return;
        this.state.importDraft = target.value;
        const scanner = this.element.querySelector('#marking-scan'); if (scanner) scanner.disabled = !this.canScan();
        const submit = this.element.querySelector('#marking-scan-form button[type=submit]'); if (submit) submit.disabled = !this.canScan();
        return;
      }
      if (target.name === 'quantity' && target.closest('.marking-quantity-form')) { this.state.quantityDrafts[target.closest('.marking-quantity-form').dataset.gtin] = target.value; return; }
      if (target.id === 'marking-sound') { this.state.sound = target.checked; if (target.checked) this.beep(true); return; }
      if (this.locked()) return;
      if (target.dataset.markingField) {
        const field = target.dataset.markingField;
        if (Object.prototype.hasOwnProperty.call(this.state.form, field) && field !== 'lines') this.state.form[field] = target.value;
        if (field === 'reference_kind') { this.state.referenceLoaded = null; this.render(); }
      } else if (target.dataset.markingLine !== undefined) {
        const line = this.state.form.lines[Number(target.dataset.markingLine)];
        if (line && ['item_code', 'gtin', 'quantity'].includes(target.dataset.markingCell)) line[target.dataset.markingCell] = target.value;
      }
    }
    keydown(event) {
      if (event.key === 'F2') { const input = this.element.querySelector('#marking-scan'); if (input && !input.disabled) { event.preventDefault(); input.focus(); } }
    }
  }
  return { Workspace, ScanStore, QUEUE_KEY, MAX_QUEUE, recovery, isMarkingCommand, validatePage, validateJob, esc };
});
