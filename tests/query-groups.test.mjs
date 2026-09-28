import {test} from 'node:test';
import assert from 'node:assert/strict';
import {queryGroups} from '../src/apptrail/static/tracking.js';

const apps = [{id: 1}, {id: 2}, {id: 3, archived: true}];
const monitors = [
  {id: 1, query: 'task manager', source: 'apple_app_store', country: 'us', app_ids: [1, 2]},
  {id: 2, query: 'task manager', source: 'google_play', country: 'gb', app_ids: [1]},
  {id: 3, query: 'task manager', source: 'google_ai_mode', country: 'us', app_ids: [1]},
  {id: 4, query: 'task manager', source: 'bing_copilot', country: 'global', app_ids: [1]},
  {id: 5, query: 'habit tracker', source: 'apple_app_store', country: 'us', app_ids: [2]},
  {id: 6, query: 'archived only', source: 'apple_app_store', country: 'us', app_ids: [3]},
];

test('filters narrow displayed variants while query controls retain the complete app scope', () => {
  const groups = queryGroups(monitors, apps, {kind: 'store', appId: 1, source: 'google_play', country: 'gb'});
  assert.equal(groups.length, 1);
  assert.deepEqual(groups[0].monitors.map(m => m.id), [1, 2]);
  assert.deepEqual(groups[0].visible.map(m => m.id), [2]);
  assert.equal(queryGroups(monitors, apps, {kind: 'store', appId: 2}).length, 2);
  assert.deepEqual(queryGroups(monitors, apps, {kind: 'store', appId: 2})[0].monitors.map(m => m.id), [1]);
});

test('AI global and country variants stay together; stores and archived-only queries stay separate', () => {
  const groups = queryGroups(monitors, apps);
  assert.equal(groups.length, 3);
  const ai = queryGroups(monitors, apps, {kind: 'ai'})[0];
  assert.deepEqual(ai.monitors.map(m => m.id), [3, 4]);
  assert.notEqual(ai.key, groups[0].key);
  const reversed = queryGroups([...monitors].reverse(), apps, {kind: 'ai'})[0];
  assert.equal(reversed.key, ai.key);
  assert.equal(reversed.id, ai.id);
});
