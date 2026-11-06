/* Выгрузка текущего исходного JSON v2 для воспроизводимой контрольной миграции. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const fake = {innerHTML: '', textContent: '', value: '', append() {}, close() {}, classList: {toggle() {}}};
const context = {document: {querySelector() {return null;}, querySelectorAll() {return [];},
  addEventListener() {}, createElement() {return fake;}},
  localStorage: {getItem() {return null;}, setItem() {}}, location: {hash: ''},
  window: {addEventListener() {}, scrollTo() {}}, console, Intl, Date, Math, Number, Set, Map, JSON,
  structuredClone, FormData: class {}, setTimeout() {}, Blob, URL};
vm.createContext(context);
const source = fs.readFileSync(path.join(__dirname, '../dist/assets/app.js'), 'utf8').replace(/^routeChanged\(\);$/m, '');
vm.runInContext(source, context);
process.stdout.write(vm.runInContext('JSON.stringify(db, null, 2)', context) + '\n');
