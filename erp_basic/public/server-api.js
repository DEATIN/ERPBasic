/* ERP Basic: Frappe на своём origin. Вкладка хранит только один неподтверждённый запрос для восстановления. */
(function (root, factory) {
  const exported = factory();
  if (typeof module === 'object' && module.exports) module.exports = exported;
  else root.ERPBasicAPI = exported;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  class ApiError extends Error {
    constructor(message, options = {}) {
      super(message);
      this.name = 'ApiError';
      Object.assign(this, { status: 0, code: 'REQUEST_FAILED', uncertain: false }, options);
    }
  }

  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === 'object') {
      return Object.fromEntries(Object.keys(value).sort().filter(key => value[key] !== undefined)
        .map(key => [key, canonical(value[key])]));
    }
    return value;
  }

  function serverMessage(data) {
    try {
      const messages = typeof data._server_messages === 'string' ? JSON.parse(data._server_messages) : data._server_messages;
      if (Array.isArray(messages) && messages.length) {
        const first = typeof messages[0] === 'string' ? JSON.parse(messages[0]) : messages[0];
        // Диагностика остаётся текстом; HTML экранируется только при выводе в интерфейс.
        if (typeof first.message === 'string') return first.message.slice(0, 800);
      }
    } catch (_) { /* Повреждённая диагностика не должна скрывать ошибку запроса. */ }
    return '';
  }

  function errorFor(response, data, mutation) {
    const payloadError = data.error || (data.message && typeof data.message === 'object' ? data.message.error : null);
    const code = data.exc_type || payloadError?.code || 'REQUEST_FAILED';
    let message = serverMessage(data) || (typeof payloadError?.message === 'string' ? payloadError.message.slice(0, 800) : '');
    if (response.status === 401 || ['AuthenticationError', 'SessionExpired'].includes(code)) {
      message = 'Сессия завершилась. Войдите снова; данные учёта остаются на сервере.';
    } else if (code === 'CSRFTokenError') {
      message = 'Сессия изменилась. Обновите страницу и войдите снова перед повтором операции.';
    } else if (response.status === 403) {
      message = 'Недостаточно прав для этой операции. Обратитесь к администратору своей организации.';
    } else if (response.status === 409 || ['TimestampMismatchError', 'ConflictError', 'IdempotencyConflict'].includes(code)) {
      message = message || 'Документ изменён другим сотрудником либо ключ операции уже использован. Загрузите актуальную версию и повторите проверку.';
    } else if (response.status === 429) {
      message = 'Слишком много запросов. Подождите немного и повторите попытку.';
    } else if (response.status >= 500) {
      message = mutation
        ? 'Сервер не подтвердил результат. Операция могла сохраниться. Повторите тот же запрос для проверки результата.'
        : 'Сервер временно недоступен. Попробуйте обновить данные позже.';
    }
    return new ApiError(message || 'Сервер отклонил запрос. Проверьте поля документа и повторите попытку.', {
      status: response.status,
      code,
      uncertain: mutation && response.status >= 500,
    });
  }

  const RECOVERY_KEY = 'erp-basic:pending-command:v1';
  const COMMAND_FIELDS = {
    save_document: ['kind', 'company', 'document', 'expected_modified'],
    submit_document: ['kind', 'name', 'company', 'expected_modified'],
    cancel_document: ['kind', 'name', 'company', 'expected_modified'],
    create_related: ['kind', 'name', 'target_kind', 'company', 'expected_modified'],
    create_return: ['kind', 'name', 'company', 'expected_modified'],
    marking_create_job: ['company', 'role', 'profile', 'operation', 'lines', 'reference_kind', 'reference_name', 'warehouse'],
    marking_scan_code: ['company', 'name', 'raw_code', 'expected_revision', 'device_id', 'event_id', 'sequence'],
    marking_complete_job: ['company', 'name', 'expected_revision'],
    marking_cancel_job: ['company', 'name', 'expected_revision', 'reason'],
    marking_record_quantity: ['company', 'name', 'gtin', 'quantity', 'expected_revision'],
    marking_import_expected_codes: ['company', 'name', 'codes', 'expected_revision'],
  };
  const MARKING_METHODS = ['workspace', 'get_job', 'get_discrepancy', 'create_job', 'scan_code', 'complete_job', 'cancel_job', 'record_quantity', 'import_expected_codes'];
  const isMarkingCommand = method => Object.prototype.hasOwnProperty.call(COMMAND_FIELDS, method) && method.startsWith('marking_');

  function storageFromBrowser() {
    try { return globalThis.sessionStorage || null; } catch (_) { return null; }
  }
  async function digest(text) {
    if (!globalThis.crypto?.subtle) throw new ApiError('Защищённое восстановление записи требует HTTPS или localhost.', { code: 'SECURE_CONTEXT_REQUIRED' });
    const value = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
    return Array.from(new Uint8Array(value), byte => byte.toString(16).padStart(2, '0')).join('');
  }
  function hasCredentialField(value) {
    if (!value || typeof value !== 'object') return false;
    return Object.entries(value).some(([key, item]) => /^(?:password|pwd|passwd|csrf_token|access_token|refresh_token|api_key|api_secret|secret|authorization|cookie)$/i.test(key) || hasCredentialField(item));
  }
  function commandText(method, params) {
    if (!Object.prototype.hasOwnProperty.call(COMMAND_FIELDS, method) || !params || typeof params !== 'object' || Array.isArray(params) ||
      Object.keys(params).some(key => !COMMAND_FIELDS[method].includes(key)) || !params.company || (!isMarkingCommand(method) && !params.kind) || hasCredentialField(params)) {
      throw new ApiError('Запрос не соответствует безопасному контракту записи.', { code: 'INVALID_COMMAND' });
    }
    return JSON.stringify(canonical({ method, params }));
  }

  class PendingStore {
    constructor(options = {}) {
      this.storage = Object.prototype.hasOwnProperty.call(options, 'storage') ? options.storage : storageFromBrowser();
      this.digest = options.digest || digest;
      this.clock = options.clock || (() => new Date().toISOString());
    }
    readRaw() {
      if (!this.storage) return null;
      let raw;
      try { raw = this.storage.getItem(RECOVERY_KEY); }
      catch (_) { throw new ApiError('Недоступна служебная запись восстановления. Разрешите хранение данных сеанса для этого сайта.', { code: 'RECOVERY_UNAVAILABLE' }); }
      if (!raw) return null;
      try {
        if (raw.length > 512 * 1024) throw new Error();
        const record = JSON.parse(raw);
        if (record?.version !== 1 || typeof record.user !== 'string' || typeof record.company !== 'string' || typeof record.key !== 'string' || !/^[a-zA-Z0-9:_-]{8,128}$/.test(record.key) || typeof record.digest !== 'string' || typeof record.created_at !== 'string' || !Number.isFinite(Date.parse(record.created_at))) throw new Error();
        return record;
      } catch (_) { throw new ApiError('Служебная запись незавершённой операции повреждена. Проверьте созданные документы на сервере перед новым вводом.', { code: 'RECOVERY_CORRUPT', uncertain: true }); }
    }
    async read(user, companies) {
      const record = this.readRaw();
      if (!record) return null;
      // При смене пользователя нельзя раскрывать запрос предыдущей учётной записи.
      if (record.user !== user) { this.clear(); return null; }
      if (!companies.has(record.company)) throw new ApiError('Доступ к организации незавершённой операции изменился. Обратитесь к администратору перед повторным вводом.', { code: 'RECOVERY_ACCESS_CHANGED', uncertain: true });
      try {
        commandText(record.method, record.params);
        const signed = JSON.stringify(canonical({ ...record, digest: undefined }));
        if (record.params.company !== record.company || await this.digest(signed) !== record.digest) throw new Error();
      } catch (_) { throw new ApiError('Данные незавершённого запроса не прошли проверку. Проверьте документ на сервере перед новым вводом.', { code: 'RECOVERY_CORRUPT', uncertain: true }); }
      return record;
    }
    async write(user, method, params, key) {
      if (!this.storage) throw new ApiError('Браузер запретил временное хранение запроса. Разрешите данные сеанса для этого сайта, чтобы запись можно было безопасно восстановить после перезагрузки.', { code: 'RECOVERY_UNAVAILABLE' });
      commandText(method, params);
      const record = { version: 1, user, company: params.company, method, params: canonical(params), key, created_at: this.clock() };
      record.digest = await this.digest(JSON.stringify(canonical(record)));
      const raw = JSON.stringify(record);
      if (raw.length > 512 * 1024) throw new ApiError('Документ слишком велик для безопасного восстановления в этой вкладке. Уменьшите число строк.', { code: 'RECOVERY_TOO_LARGE' });
      try { this.storage.setItem(RECOVERY_KEY, raw); if (this.storage.getItem(RECOVERY_KEY) !== raw) throw new Error(); }
      catch (_) { throw new ApiError('Не удалось сохранить служебный ключ восстановления. Запрос на сервер не отправлен. Освободите хранилище сеанса и повторите.', { code: 'RECOVERY_UNAVAILABLE' }); }
      return record;
    }
    clear() {
      if (!this.storage) return;
      try { this.storage.removeItem(RECOVERY_KEY); }
      catch (_) { throw new ApiError('Не удалось очистить запись восстановления. Повторите тот же запрос перед следующей операцией.', { code: 'RECOVERY_CLEANUP_FAILED', uncertain: true }); }
    }
  }

  class Client {
    constructor(options = {}) {
      this.fetch = options.fetch || rootFetch();
      this.timeout = options.timeout || 30000;
      this.csrfToken = '';
      this.pending = new Map();
      this.user = '';
      this.companies = new Set();
      this.inFlight = false;
      this.store = new PendingStore(options);
      this.createKey = options.createKey || (() => {
        if (!globalThis.crypto?.randomUUID) throw new ApiError('Для защищённых операций требуется HTTPS или localhost.', { code: 'SECURE_CONTEXT_REQUIRED' });
        return globalThis.crypto.randomUUID();
      });
    }

    async request(method, params = {}, options = {}) {
      const verb = options.verb || 'GET';
      const mutation = Boolean(options.mutation);
      const builtin = options.builtin === true;
      const namespace = options.namespace || 'api';
      if (!/^[a-z_]+$/.test(method) || !['api', 'integration', 'marking'].includes(namespace) || (namespace === 'integration' && !['journal', 'retry_inbox', 'retry_outbox'].includes(method)) || (namespace === 'marking' && !MARKING_METHODS.includes(method)) || (builtin && (!['login', 'logout'].includes(method) || namespace !== 'api'))) {
        throw new ApiError('Некорректный метод API.', { code: 'INVALID_METHOD' });
      }
      const base = builtin ? '/api/method/' : `/api/method/erp_basic.${namespace === 'marking' ? 'marking.api' : namespace}.`;
      let url = base + method;
      const headers = { Accept: 'application/json' };
      const init = { method: verb, headers, credentials: 'same-origin', cache: 'no-store' };
      if (verb === 'GET') {
        const query = new URLSearchParams();
        Object.entries(params).forEach(([key, value]) => {
          if (value !== undefined && value !== null) query.set(key, typeof value === 'object' ? JSON.stringify(value) : String(value));
        });
        if (query.size) url += '?' + query.toString();
      } else {
        headers['Content-Type'] = 'application/json';
        if (this.csrfToken) headers['X-Frappe-CSRF-Token'] = this.csrfToken;
        init.body = JSON.stringify(params);
      }
      const controller = new AbortController();
      init.signal = controller.signal;
      const timer = setTimeout(() => controller.abort(), this.timeout);
      try {
        const response = await this.fetch(url, init);
        let data;
        try { data = await response.json(); }
        catch (_) {
          throw new ApiError(mutation
            ? 'Ответ сервера не удалось прочитать. Операция могла сохраниться. Повторите тот же запрос.'
            : 'Сервер вернул неожиданный ответ. Проверьте адрес ERP Basic и доступность API.', {
            status: response.status, code: 'INVALID_RESPONSE', uncertain: mutation,
          });
        }
        if (!data || typeof data !== 'object' || Array.isArray(data)) throw new ApiError('API вернул неверный формат ответа.', { status: response.status, code: 'INVALID_RESPONSE', uncertain: mutation });
        if (!response.ok || data.exc_type || data.exception || data.error || (data.message && typeof data.message === 'object' && data.message.error)) {
          const error = errorFor(response, data, mutation);
          // Frappe возвращает 403 и гостю, и сотруднику без нужного права.
          // Один запрос сессии различает эти случаи без повтора отклонённой записи.
          if (response.status === 403 && !builtin && method !== 'session' && options.checkSession !== false) {
            try {
              const current = await this.request('session', {}, { checkSession: false });
              if (current?.authenticated === false) {
                error.code = 'SessionExpired';
                error.message = 'Сессия завершилась. Войдите снова; данные учёта остаются на сервере.';
              }
            } catch (_) { /* При недоступной сессии сохраняется исходная ошибка доступа. */ }
          }
          throw error;
        }
        if (builtin && method === 'logout' && !Object.prototype.hasOwnProperty.call(data, 'message')) return null;
        if (!Object.prototype.hasOwnProperty.call(data, 'message')) {
          throw new ApiError('API вернул ответ без результата. Обновите данные с сервера.', {
            status: response.status, code: 'INVALID_RESPONSE', uncertain: mutation,
          });
        }
        return data.message;
      } catch (error) {
        if (error instanceof ApiError) throw error;
        throw new ApiError(mutation
          ? 'Связь прервалась до подтверждения результата. Повторите тот же запрос: сервер проверит ключ операции.'
          : 'Не удалось связаться с сервером. Проверьте подключение и повторите загрузку.', {
          code: error.name === 'AbortError' ? 'TIMEOUT' : 'NETWORK_ERROR', uncertain: mutation,
        });
      } finally { clearTimeout(timer); }
    }

    async session() {
      const session = await this.request('session');
      this.csrfToken = session?.csrf_token || '';
      this.bindSession(session);
      return session;
    }

    async login(username, password) {
      await this.request('login', { usr: username, pwd: password }, { verb: 'POST', builtin: true });
      return this.session();
    }

    async logout() {
      await this.request('logout', {}, { verb: 'POST', builtin: true });
      this.csrfToken = '';
      this.user = '';
      this.companies.clear();
      this.pending.clear();
      this.store.clear();
    }

    bindSession(session) {
      const nextUser = session?.authenticated && typeof session.user === 'string' ? session.user : '';
      if (nextUser !== this.user) this.pending.clear();
      this.user = nextUser;
      this.companies = new Set((Array.isArray(session?.companies) ? session.companies : []).map(company => company.name));
      if (!this.user) this.pending.clear();
    }

    async recovery() {
      if (!this.user) return null;
      const record = await this.store.read(this.user, this.companies);
      if (record) this.pending.set(commandText(record.method, record.params), { ...record, uncertain: true });
      return record;
    }

    async command(method, params, options = {}) {
      if (this.inFlight) throw new ApiError('Дождитесь завершения текущего запроса.', { code: 'REQUEST_IN_PROGRESS' });
      const signature = commandText(method, params);
      if (!this.user || !this.companies.has(params.company)) throw new ApiError('Войдите в систему и выберите доступную организацию.', { code: 'AuthenticationError', status: 401 });
      this.inFlight = true;
      let record;
      try {
        const saved = await this.store.read(this.user, this.companies);
        if (saved && commandText(saved.method, saved.params) !== signature) throw new ApiError('Сначала подтвердите результат предыдущего запроса. Новый документ пока не отправлен.', { code: 'RECOVERY_REQUIRED' });
        record = this.pending.get(signature) || (saved ? { ...saved, uncertain: true } : null);
        if (!record) record = { ...await this.store.write(this.user, method, params, this.createKey()), uncertain: false };
        this.pending.set(signature, record);
        const result = await this.request(isMarkingCommand(method) ? method.slice(8) : method, { ...record.params, idempotency_key: record.key }, { verb: 'POST', mutation: true, namespace: isMarkingCommand(method) ? 'marking' : 'api' });
        if (options.validate) await options.validate(result);
        this.store.clear();
        this.pending.delete(signature);
        return result;
      } catch (error) {
        if (record) {
          if (error.uncertain || record.uncertain) {
            // Последующий отказ в доступе не определяет результат исходного потерянного ответа.
            record.uncertain = true;
            error.uncertain = true;
          } else {
            try { this.store.clear(); this.pending.delete(signature); }
            catch (cleanupError) { record.uncertain = true; throw cleanupError; }
          }
        }
        throw error;
      } finally { this.inFlight = false; }
    }

  }

  function rootFetch() {
    if (typeof globalThis.fetch !== 'function') throw new ApiError('Браузер не поддерживает сетевые запросы.', { code: 'FETCH_UNAVAILABLE' });
    return globalThis.fetch.bind(globalThis);
  }

  return { ApiError, Client, PendingStore, RECOVERY_KEY, canonical, isMarkingCommand };
});
