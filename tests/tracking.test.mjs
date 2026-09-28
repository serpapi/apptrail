import {test} from 'node:test';
import assert from 'node:assert/strict';
import {expandQueries, searchUsage} from '../src/apptrail/static/tracking.js';
const base = {sources:['apple_app_store', 'google_play'], countries:['us','gb','in'], storeQueries:['task manager'], aiQueries:[], language:'en', frequency:'daily', depth:2, device:'', appIds:[1]};
test('three countries create six store checks and estimate pagination credits', () => {
  const monitors = expandQueries(base);
  assert.equal(monitors.length, 6);
  assert.deepEqual(new Set(monitors.map(m => m.country)), new Set(['us','gb','in']));
  assert.deepEqual(searchUsage(monitors), {created:6,min:270,max:270});
});
test('AI countries keep one global Copilot check and account for overview follow-ups', () => {
  const monitors = expandQueries({...base, sources:['google_ai_mode','google_ai_overview','bing_copilot'], aiQueries:['Best task apps?']});
  assert.equal(monitors.length,7);
  assert.ok(monitors.every(m => m.depth === 1));
  assert.deepEqual(searchUsage(monitors), {created:7,min:210,max:300});
});
test('repeated queries, countries and sources do not inflate checks', () => {
  const monitors = expandQueries({...base, countries:['us','us'], storeQueries:['task manager',' task manager '], sources:['google_play','google_play']});
  assert.equal(monitors.length, 1);
});
test('existing normalized Copilot and Play searches add no credits', () => {
  const monitors = expandQueries({...base, sources:['google_play','bing_copilot'], aiQueries:['Best task apps?'], device:'tablet'});
  const existing = monitors.map(m => ({...m, device:'', ...(m.source==='bing_copilot' ? {country:'global',language:'auto'} : {})}));
  assert.deepEqual(searchUsage(monitors, existing), {created:0,min:0,max:0});
  assert.deepEqual(searchUsage(expandQueries({...base,countries:['fr']}), monitors), {created:2,min:90,max:90});
});
test('regional searches need a country while global questions do not', () => {
  assert.throws(() => expandQueries({...base,countries:[]}), /Select at least one country/);
  assert.equal(expandQueries({...base,countries:[],sources:['bing_copilot'],aiQueries:['Best apps?']}).length,1);
});
test('all supported countries can be selected; oversized query batches fail before saving', () => {
  const countries = Array.from({length:244},(_,i)=>String(i));
  assert.equal(expandQueries({...base,countries}).length,488);
  assert.throws(() => expandQueries({...base,countries,storeQueries:['a','b','c','d','e']}), /2,000/);
});
