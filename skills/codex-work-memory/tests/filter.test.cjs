'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const helpers = require('../assets/archive.js');
const { normalise, validDay, dateError, matchesRevision, selectItems, presetBounds } = helpers;

const base = { query: '', project: '', from: '', to: '', status: '', sort: 'newest' };
const filter = changes => ({ ...base, ...changes });
const rev = (day, at, status, search) => ({ day, stamp: Date.parse(at), status, search });
// Explicit Shanghai dates straddle local midnight, while both timestamps are UTC October 22.
const a = { id: 'a', project: 'alpha', projectName: 'Alpha', revisions: [
  rev('2026-10-23', '2026-10-22T16:00:00Z', '已完成', 'HTTP 连接已通 final-fix.md'),
  rev('2026-10-22', '2026-10-22T15:59:59.999Z', '受阻', 'UDP 探测失败 C:/logs/old-probe.md 原会话网络诊断'),
] };
const b = { id: 'b', project: 'beta', projectName: 'Beta', revisions: [
  rev('2026-10-23', '2026-10-23T04:00:00Z', '进行中', 'UDP 探测优化 [x]+ (GA)'),
] };
const items = [a, b];
const ids = values => values.map(value => value.item.id);

test('normalise supports case and full-width compatibility without trimming source content', () => {
  assert.equal(normalise('ＧＡ ＵＤＰ'), 'ga udp');
  assert.equal(normalise(undefined), '');
});

test('validDay rejects rollover, invalid widths, and impossible dates; supports leap years', () => {
  for (const day of ['2026-02-29', '2026-04-31', '2026-13-01', '2026-00-10', '2026-1-01', '', 'invalid']) {
    assert.equal(validDay(day), false, day);
  }
  for (const day of ['2024-02-29', '2026-10-23', '2000-02-29']) assert.equal(validDay(day), true, day);
});

test('invalid and reversed ranges are rejected, but one-sided ranges remain valid', () => {
  assert.notEqual(dateError(filter({ from: '2026-02-30' })), '');
  assert.notEqual(dateError(filter({ to: 'abc' })), '');
  assert.notEqual(dateError(filter({ from: '2026-10-24', to: '2026-10-23' })), '');
  assert.equal(dateError(filter({ from: '2026-10-23' })), '');
  assert.equal(dateError(filter({ to: '2026-10-23' })), '');
  assert.deepEqual(selectItems(items, filter({ from: '2026-10-24', to: '2026-10-23' })), []);
});

test('same-day filters include the full Shanghai day and exclude next midnight', () => {
  const day22 = selectItems([a], filter({ from: '2026-10-22', to: '2026-10-22' }));
  assert.equal(day22.length, 1);
  assert.equal(day22[0].index, 1);
  assert.equal(day22[0].revision.stamp, Date.parse('2026-10-22T15:59:59.999Z'));
  const day23 = selectItems([a], filter({ from: '2026-10-23', to: '2026-10-23' }));
  assert.equal(day23[0].index, 0);
  assert.equal(day23[0].matches, 1);
});

test('keywords and dates cannot match different revisions of one item', () => {
  assert.deepEqual(selectItems([a], filter({ query: 'UDP', from: '2026-10-23', to: '2026-10-23' })), []);
  const old = selectItems([a], filter({ query: 'udp', from: '2026-10-22', to: '2026-10-22' }));
  assert.equal(old[0].index, 1);
  assert.equal(old[0].revision.status, '受阻');
});

test('keywords and status cannot match different revisions', () => {
  assert.deepEqual(selectItems([a], filter({ query: 'UDP', status: '已完成' })), []);
  assert.equal(selectItems([a], filter({ query: 'UDP', status: '受阻' }))[0].index, 1);
});

test('all query terms must match the same revision', () => {
  assert.deepEqual(selectItems([a], filter({ query: 'HTTP UDP' })), []);
  assert.equal(selectItems([a], filter({ query: ' udp\n old-probe.md\t诊断 ' }))[0].index, 1);
});

test('search remains literal for punctuation and normalises full-width queries', () => {
  assert.equal(matchesRevision(b.revisions[0], filter({ query: '[x]+ (GA)' })), true);
  assert.equal(matchesRevision(b.revisions[0], filter({ query: '.*' })), false);
  assert.equal(selectItems([b], filter({ query: 'ＵＤＰ' })).length, 1);
});

test('deep filename and source title terms can match the supplied search text', () => {
  assert.equal(selectItems([a], filter({ query: 'C:/logs/old-probe.md' }))[0].index, 1);
  assert.equal(selectItems([a], filter({ query: '原会话网络诊断' }))[0].index, 1);
});

test('project selection matches IDs even when display names are the same', () => {
  const duplicateName = { ...b, projectName: 'Alpha' };
  assert.deepEqual(ids(selectItems([a, duplicateName], filter({ project: 'beta' }))), ['b']);
  assert.deepEqual(selectItems([a, duplicateName], filter({ project: 'missing' })), []);
});

test('unfiltered results choose latest revisions and count matching progress separately', () => {
  const result = selectItems(items, filter());
  assert.deepEqual(ids(result), ['b', 'a']);
  const foundA = result.find(value => value.item.id === 'a');
  assert.equal(foundA.index, 0);
  assert.equal(foundA.matches, 2);
  assert.equal(result.length, 2);
});

test('sorting uses the displayed historical match, not a non-matching latest update', () => {
  const futureA = { ...a, revisions: [
    rev('2026-10-24', '2026-10-24T01:00:00Z', '已完成', 'HTTP final'),
    a.revisions[1],
  ] };
  assert.deepEqual(ids(selectItems([futureA, b], filter({ query: 'UDP' }))), ['b', 'a']);
  const ascending = selectItems([futureA, b], filter({ query: 'UDP', sort: 'oldest' }));
  assert.deepEqual(ids(ascending), ['a', 'b']);
  assert.equal(ascending[0].index, 1);
});

test('oldest ordering still selects the latest matching revision per item', () => {
  const result = selectItems(items, filter({ sort: 'oldest' }));
  assert.deepEqual(ids(result), ['a', 'b']);
  assert.equal(result[0].index, 0);
});

test('project sorting groups by name then matches latest first within project', () => {
  const alphaLater = { ...b, id: 'c', project: 'alpha', projectName: 'Alpha' };
  assert.deepEqual(ids(selectItems([b, a, alphaLater], filter({ sort: 'project' }))), ['c', 'a', 'b']);
});

test('equal timestamps have stable ID tie order', () => {
  const z = { ...b, id: 'z' };
  assert.deepEqual(ids(selectItems([z, b], filter())), ['b', 'z']);
});

test('empty inputs, no revision entries, blank queries and missing search strings are safe', () => {
  assert.deepEqual(selectItems([], filter()), []);
  assert.deepEqual(selectItems([{ ...a, revisions: [] }], filter()), []);
  assert.deepEqual(ids(selectItems(items, filter({ query: ' \n\t ' }))), ['b', 'a']);
  const withoutSearch = { ...b, revisions: [{ ...b.revisions[0], search: undefined }] };
  assert.equal(selectItems([withoutSearch], filter()).length, 1);
  assert.deepEqual(selectItems([withoutSearch], filter({ query: 'udp' })), []);
});

test('one-sided ranges do not invent a second endpoint', () => {
  assert.equal(selectItems([a], filter({ to: '2026-10-22' }))[0].index, 1);
  assert.equal(selectItems([a], filter({ from: '2026-10-23' }))[0].index, 0);
  assert.deepEqual(selectItems(items, filter({ from: '2026-10-24' })), []);
});

test('preset calendar windows include today and cross months and years correctly', () => {
  assert.deepEqual(presetBounds('7', '2026-01-03'), { from: '2025-12-28', to: '2026-01-03' });
  assert.deepEqual(presetBounds('30', '2024-03-01'), { from: '2024-02-01', to: '2024-03-01' });
  assert.deepEqual(presetBounds('week', '2026-10-25'), { from: '2026-10-19', to: '2026-10-25' });
  assert.deepEqual(presetBounds('week', '2026-10-26'), { from: '2026-10-26', to: '2026-10-26' });
  assert.deepEqual(presetBounds('all', '2026-10-24'), { from: '', to: '' });
  assert.deepEqual(presetBounds('unknown', '2026-10-24'), { from: '', to: '' });
  assert.deepEqual(presetBounds('7', 'bad'), { from: '', to: '' });
});

test('filtering and sorting never mutate source items or revisions', () => {
  const frozen = structuredClone(items);
  frozen.forEach(item => {
    item.revisions.forEach(Object.freeze);
    Object.freeze(item.revisions);
    Object.freeze(item);
  });
  Object.freeze(frozen);
  const before = JSON.stringify(frozen);
  selectItems(frozen, filter({ query: 'UDP', sort: 'oldest' }));
  assert.equal(JSON.stringify(frozen), before);
});
