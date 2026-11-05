const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function app(){
 const storage=new Map();
 const output={toast:[],hash:'',saved:0};
 const fake={innerHTML:'',textContent:'',value:'',append(){},close(){},classList:{toggle(){}}};
 const document={querySelector(){return null},querySelectorAll(){return[]},addEventListener(){},createElement(){return fake}};
 const ctx={document,localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},location:{hash:''},window:{addEventListener(){},scrollTo(){}},console,Intl,Date,Math,Number,Set,Map,JSON,structuredClone,FormData:class{},setTimeout(){},Blob,URL};
 vm.createContext(ctx);
 const code=fs.readFileSync(require('node:path').join(__dirname,'../dist/assets/app.js'),'utf8').replace(/^routeChanged\(\);$/m,'');
 vm.runInContext(code,ctx);
 vm.runInContext("toast=(text,error)=>{};render=()=>{};renderDocument=()=>{};captureDraft=()=>{};",ctx);
 return {run:s=>vm.runInContext(s,ctx),storage};
}
test('Скидка рассчитывается по нескольким строкам',()=>{const a=app();assert.equal(a.run("calc({lines:[{qty:2,rate:28000},{qty:1,rate:850}],discount:10})"),51165)});
test('Аванс и возврат изменяют долг, не изменяя сумму заказа',()=>{const a=app();assert.equal(a.run("db.orders=[{id:'TEST',lines:[{qty:1,rate:10000}],discount:0}];db.payments=[{source:'TEST',amount:3000,direction:'in'},{source:'TEST',amount:500,direction:'out'}];due(db.orders[0])"),7500);assert.equal(a.run('calc(db.orders[0])'),10000)});
test('Повтор подтверждения поступления не увеличивает остаток дважды',()=>{const a=app();a.run("route={module:'buying',page:'receipts',id:'REC-TEST'};db.items[0].qty=10;draft={id:'REC-TEST',partner:'S-001',date:'2026-11-05',delivery:'2026-11-05',lines:[{item:'MAT-001',qty:4,rate:850}],state:'draft',discount:0,history:[]};db.receipts=[structuredClone(draft)];saveDocument(true)");assert.equal(a.run("item('MAT-001').qty"),14);a.run('saveDocument(true)');assert.equal(a.run("item('MAT-001').qty"),14)});
test('Недостаток товара учитывает все повторяющиеся строки',()=>{const a=app();a.run("route={module:'sales',page:'invoices',id:'INV-TEST'};db.items[0].qty=5;draft={id:'INV-TEST',partner:'C-001',date:'2026-11-05',delivery:'2026-11-05',lines:[{item:'MAT-001',qty:3,rate:850},{item:'MAT-001',qty:3,rate:850}],state:'draft',discount:0,history:[]};db.invoices=[structuredClone(draft)];saveDocument(true)");assert.equal(a.run("item('MAT-001').qty"),5);assert.equal(a.run('db.invoices[0].state'),'draft')});
test('Реализация уменьшает остаток ровно один раз',()=>{const a=app();a.run("route={module:'sales',page:'invoices',id:'INV-TEST'};db.items[0].qty=10;draft={id:'INV-TEST',partner:'C-001',date:'2026-11-05',delivery:'2026-11-05',lines:[{item:'MAT-001',qty:3,rate:850}],state:'draft',discount:0,history:[]};db.invoices=[structuredClone(draft)];saveDocument(true);saveDocument(true)");assert.equal(a.run("item('MAT-001').qty"),7)});
test('CSV сохраняет разделители и переводы строк внутри кавычек',()=>{const a=app();assert.equal(a.run('parseCsv(\'Код;Название\\nA;"Позиция; один"\\nB;"две\\nстроки"\')[2][1]'),'две\nстроки')});
test('Названия безопасно экранируются для HTML и выгрузки CSV',()=>{const a=app();assert.equal(a.run('esc(\'<img src=x onerror="alert(1)">\')'),'&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');assert.match(a.run('csvValue("=SUM(1,2)")'),/^"'/)});
