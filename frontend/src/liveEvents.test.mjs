import test from 'node:test';
import assert from 'node:assert/strict';
import {applyNotebookDelta,mergeRemoteOutput} from './liveEvents.mjs';
const before={cells:[{id:'a',cell_type:'markdown',source:'hello'},{id:'b',cell_type:'code',source:'print(1)',outputs:[],execution_count:null}],metadata:{language_info:{name:'python'}}};
test('add/move/delete delta',()=>{const doc=applyNotebookDelta(before,{order:['c','b'],changed:[{id:'c',cell_type:'code',source:['print(2)'],outputs:[],execution_count:null}],removed:['a']},2,3);assert.deepEqual(doc.cells.map(c=>c.id),['c','b']);assert.equal(doc.cells[0].source,'print(2)');assert.deepEqual(doc.metadata,before.metadata);});
test('replays and version gaps cause full reconciliation',()=>{const p={order:['a','b'],changed:[],removed:[]};assert.equal(applyNotebookDelta(before,p,2,2),null);assert.equal(applyNotebookDelta(before,p,2,4),null);assert.equal(applyNotebookDelta(before,{full_sync:true},2,3),null);});
test('invalid delta does not corrupt local snapshot',()=>{assert.equal(applyNotebookDelta(before,{order:['b','b'],removed:['a'],changed:[]},2,3),null);assert.equal(applyNotebookDelta(before,{order:['x','b'],removed:[],changed:[]},2,3),null);});
test('run output only updates affected Cell and is bounded',()=>{const next=mergeRemoteOutput(before,'b',[{output_type:'stream',text:'step 1'}]);assert.equal(next.cells[1].outputs[0].text,'step 1');assert.equal(next.cells[0],before.cells[0]);assert.equal(mergeRemoteOutput(before,'missing',[]),null);assert.equal(mergeRemoteOutput(before,'b',Array(101).fill({})),null);});
