'use strict';

const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const ledger = require('./ledger');

const STATE_PATH = '/tmp/autoforge-stateful-ledger.json';
const MAX_STATE_BYTES = 128;

function loadState() {
  try {
    const bytes = fs.readFileSync(STATE_PATH);
    if (bytes.length > MAX_STATE_BYTES) throw new Error('oversized state');
    const value = JSON.parse(bytes.toString('utf8'));
    if (!value || !Number.isSafeInteger(value.balance) || value.balance < 0 || value.balance > ledger.LIMIT ||
        !Number.isSafeInteger(value.revision) || value.revision < 0) throw new Error('invalid state');
    return value;
  } catch (_error) {
    return { balance: 0, revision: 0 };
  }
}

function saveState(state) {
  const tempPath = path.join(path.dirname(STATE_PATH), `.autoforge-ledger-${process.pid}.tmp`);
  const bytes = Buffer.from(JSON.stringify(state));
  if (bytes.length > MAX_STATE_BYTES) throw new Error('oversized state');
  try {
    fs.writeFileSync(tempPath, bytes, { mode: 0o600 });
    fs.renameSync(tempPath, STATE_PATH);
  } finally {
    try { fs.unlinkSync(tempPath); } catch (_error) {}
  }
}

let state = loadState();
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on('line', (line) => {
  let response;
  try {
    const request = JSON.parse(line);
    const next = ledger.nextState(state, request);
    if (next) {
      saveState(next);
      state = next;
      response = { balance: state.balance, revision: state.revision };
    } else {
      response = ledger.apply(state, request);
    }
  } catch (_error) {
    response = { error: 'invalid' };
  }
  process.stdout.write(JSON.stringify(response) + '\n');
});
