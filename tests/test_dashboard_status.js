// Exercise the actual dashboard classifier without sending any network requests.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('scanner_app/index.html', 'utf8');
const start = source.indexOf('    function rollcallStatus(account) {');
const end = source.indexOf('    // Render Dashboard Cards', start);
const scope = {};
vm.runInNewContext(source.slice(start, end), scope);
const base = {monitor_state: 'running', cookie_valid: true,
    last_check: {status: 'on_call_fine', timestamp: 100, age_seconds: 2}};
function classify(changes) { return scope.rollcallStatus({...base, ...changes}); }
assert.equal(classify({}).tone, 'confirmed');
assert.equal(classify({service_offline: true}).tone, 'warn');
assert.equal(classify({last_login: {status: 'rejected', ok: false, timestamp: 101}}).label, '登入尚未完成');
assert.equal(classify({last_check: {...base.last_check, age_seconds: 30}}).tone, 'warn');
assert.equal(classify({monitor_state: 'standby'}).label, '排程外休眠');
assert.equal(classify({paused: true}).tone, 'paused');
assert.equal(classify({heartbeat_stale: true}).label, '監控已中斷');
const crash = classify({last_error: {status: 'fatal_error', timestamp: 101, message: "reserved key 'SameSite'"}});
assert.match(crash.explanation, /登入資料格式/);
assert.match(crash.action, /官方出席紀錄/);
assert.equal(crash.tone, 'warn');
assert.equal(classify({last_error: {status: 'network_error', timestamp: 101}}).label, '暫時連不上學校');
assert.equal(classify({last_error: {status: 'unauthorized', timestamp: 101}}).label, '需要重新登入');
assert.equal(classify({last_error: {status: 'fatal_error', timestamp: 99}}).tone, 'confirmed');
assert.equal(classify({last_check: {...base.last_check, status: 'submitted_unconfirmed'}}).tone, 'warn');
assert.equal(classify({last_check: {...base.last_check, status: 'is_number', rollcall_id: '88'}, last_submission: {status: 'submitted_unconfirmed', rollcall_id: '88'}}).label, '已送出，等待確認');
assert.equal(classify({last_check: {...base.last_check, status: 'is_number', rollcall_id: '89'}, last_submission: {status: 'submitted_unconfirmed', rollcall_id: '88'}}).label, '等待數字碼');
assert.equal(classify({last_check: {...base.last_check, status: 'is_number'}}).label, '等待數字碼');
assert.equal(classify({last_check: {...base.last_check, status: 'unsupported_qrcode'}}).label, '等待 QR Code');
assert.equal(classify({last_check: {...base.last_check, status: 'unknown_state'}}).tone, 'warn');
console.log('Dashboard state scenarios passed');
