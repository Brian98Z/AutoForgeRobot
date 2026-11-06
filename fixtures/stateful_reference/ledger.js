'use strict';

const LIMIT = 1000000;

function nextState(state, request) {
  if (!request || typeof request !== 'object' || Array.isArray(request)) return null;
  if (request.op === 'credit' && Number.isSafeInteger(request.amount) && request.amount > 0 && state.balance + request.amount <= LIMIT) {
    return { balance: state.balance + request.amount, revision: state.revision + 1 };
  }
  if (request.op === 'debit' && Number.isSafeInteger(request.amount) && request.amount > 0 && state.balance >= request.amount) {
    return { balance: state.balance - request.amount, revision: state.revision + 1 };
  }
  return null;
}

function apply(state, request) {
  if (!request || typeof request !== 'object' || Array.isArray(request)) return { error: 'invalid' };
  if (request.op === 'inspect') return { balance: state.balance, revision: state.revision };
  return { error: 'invalid' };
}

module.exports = { apply, nextState, LIMIT };
