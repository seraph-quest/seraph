import assert from 'node:assert/strict';
import test from 'node:test';
import { Context, FiberState } from 'cordis';
import { readFileSync } from 'node:fs';
import { Composition, validateProfile } from '../src/composition.js';
import { decodeJson, validateFrame } from '../src/protocol.js';
import { invokeService, servicePlugins, SERVICE_KEYS } from '../src/plugins/index.js';
import { SERVICE_METHODS, validateInput, validateResult, type Input, type ServiceMethod } from '../src/contracts/methods.js';
import type { ScopedRequestClient } from '../src/contracts/client.js';

const blocked = {status: 'blocked', reason_code: 'native_variant_not_supported', memory_status: 'no_learning'};
const profile = () => validateProfile(decodeJson(readFileSync(new URL('../../profile.json', import.meta.url))));
const input: Record<ServiceMethod, unknown> = {
  'authority.resolve': {}, 'goals.read': {},
  'tasks.admit': {request_ref:'native-1'}, 'tasks.inspect':{job_ref:'native-1'}, 'tasks.cancel':{job_ref:'native-1',expected_revision:1}, 'tasks.checkpoint':{checkpoint_ref:'native-1'}, 'tasks.settle':{outcome_ref:'native-1'},
  'capabilities.list':{cursor:null,limit:10}, 'capabilities.describe':{capability_id:'work.json-format.v1'}, 'capabilities.invoke':{request_ref:'native-1'}, 'inference.request':{request_ref:'native-1'},
  'memory.retrieve':{query_ref:'native-1',limit:10}, 'memory.propose':{request_ref:'native-1'}, 'memory.applyReviewed':{review_ref:'native-1'}, 'memory.forget':{request_ref:'native-1'},
  'artifacts.read':{artifact_ref:'native-1',max_bytes:4096}, 'artifacts.stage':{request_ref:'native-1'}, 'artifacts.adopt':{request_ref:'native-1'}, 'audit.append':{event_ref:'native-1'},
  'research.buildPlan':{task_ref:'native-1'}, 'research.executeAccepted':{plan_ref:'native-1'},
  'conversation.accept':{turn_ref:'native-1'}, 'conversation.append':{message_ref:'native-1'}, 'conversation.read':{conversation_ref:'native-1',limit:10,before_message_ref:null}, 'conversation.cancel':{turn_ref:'native-1',expected_revision:1},
  'scheduler.register':{request_ref:'native-1'}, 'scheduler.disable':{schedule_ref:'native-1',expected_revision:1}, 'scheduler.dispatchDue':{limit:10},
  'connections.inspect':{connection_ref:'native-1'}, 'connections.invokeBoundAdapter':{operation_ref:'native-1'},
  'agent-loop.startTurn':{turn_ref:'native-1'}, 'agent-loop.cancelTurn':{turn_ref:'native-1',expected_revision:1}, 'agent-loop.inspectTurn':{turn_ref:'native-1'},
  'source-extraction.extract':{artifact_ref:'native-1',acquisition_receipt_ref:'receipt-1',source_slot:0,first_line:1,last_line:3},
};
for (const method of SERVICE_METHODS) test(`real scoped Cordis provider forwards ${method} once and preserves native block`, async () => {
  const calls: unknown[] = [];
  const client: ScopedRequestClient = {isActive: () => true, request: async (name, value) => {calls.push([name,value]); return blocked;}};
  const context = new Context(); context.logger.bufferSize = 0;
  const fibers = await Promise.all(Object.values(servicePlugins(client)).map(plugin => context.plugin(plugin, {})));
  assert.equal(calls.length,0, 'constructors must have no native effects');
  assert.ok(fibers.every(fiber => fiber.state === FiberState.ACTIVE));
  assert.ok(SERVICE_KEYS.every(key => context.get(key)));
  assert.deepEqual(await invokeService(context, method, input[method] as Input<typeof method>), blocked);
  assert.deepEqual(calls, [[method,input[method]]]);
  await Promise.all(fibers.map(fiber => fiber.dispose()));
  assert.equal(context.registry.size,0);
  assert.ok(SERVICE_KEYS.every(key => !context.get(key)));
});
test('positive native projections preserve artifact readback and honest memory mutation statuses', () => {
  const sha = 'a'.repeat(64);
  assert.equal(validateResult('artifacts.read',{status:'succeeded',memory_status:'no_learning',value:{artifact_ref:'native-1',digest:sha,size_bytes:2,content:'ok'}}).status,'succeeded');
  assert.equal(validateResult('tasks.admit',{status:'succeeded',memory_status:'no_learning',value:{job_ref:'native-1',revision:1,state:'accepted',replayed:true}}).status,'succeeded');
  for (const [method, status] of [['memory.propose','proposal_only'],['memory.applyReviewed','reviewed_update'],['memory.forget','forgotten']] as const) {
    const value = method === 'memory.propose' ? {proposal_ref:'proposal-1',revision:1,state:'proposed'} : {record_ref:'memory-1',revision:2};
    assert.equal(validateResult(method,{status:'succeeded',memory_status:status,value}).memory_status,status);
    assert.throws(() => validateResult(method,{status:'succeeded',memory_status:'no_learning',value}));
  }
});
test('closed requests deny client authority, URL/path/bytes, unsafe limits and asserted extraction provenance before native contact', () => {
  for (const method of SERVICE_METHODS) for (const field of ['root_id','goal_id','route','budget','deadline_at','composition_epoch','owner_kind','authority_mode']) assert.throws(() => validateInput(method,{...input[method] as object,[field]:'injected'}));
  for (const artifact_ref of ['/private/path','https://example.com','x'.repeat(129)]) assert.throws(() => validateInput('artifacts.read',{artifact_ref,max_bytes:1}));
  for (const max_bytes of [0,65537,1.5,Number.MAX_SAFE_INTEGER+1]) assert.throws(() => validateInput('artifacts.read',{artifact_ref:'native-1',max_bytes}));
  assert.throws(() => validateInput('source-extraction.extract',{...input['source-extraction.extract'] as object,raw_bytes:'secret'}));
  assert.throws(() => validateInput('source-extraction.extract',{...input['source-extraction.extract'] as object,first_line:4,last_line:3}));
  assert.throws(() => validateResult('source-extraction.extract',{status:'succeeded',memory_status:'no_learning',value:{artifact_ref:'native-1',input_digest:'a'.repeat(64),provider_digest:'b'.repeat(64),config_digest:'c'.repeat(64),evidence:[{source_ref:'source:0',text:'é'.repeat(2049)}]}}));
});
test('scope disposal and expiry fence retained providers and late native responses', async () => {
  const composition = new Composition(profile()); await composition.start();
  let active = true, release: ((value:unknown)=>void) | undefined;
  const client: ScopedRequestClient = {isActive:()=>active,request:()=>new Promise(resolve=>{release=resolve;})};
  const waiting = composition.invoke('goals.read',{},client);
  await new Promise(resolve=>setImmediate(resolve));
  active = false; release?.(blocked);
  await assert.rejects(waiting,/unavailable/);
  assert.equal(composition.resources.remaining,16); // host14 services + logger/lifecycle.
  await composition.dispose(); assert.equal(composition.resources.remaining,0);
});
test('concurrent scopes retain independent original request clients and register no ambient authority', async () => {
  const composition = new Composition(profile()); await composition.start();
  const calls:string[]=[];
  const make = (name:string):ScopedRequestClient=>({isActive:()=>true,request:async(method)=>{calls.push(`${name}:${method}`);return blocked;}});
  assert.deepEqual(await Promise.all([composition.invoke('goals.read',{},make('first')),composition.invoke('tasks.inspect',{job_ref:'job-2'},make('second'))]),[blocked,blocked]);
  assert.deepEqual(calls.sort(),['first:goals.read','second:tasks.inspect']);
  await assert.rejects(composition.context.seraphGoals.read({}),/unavailable/);
  await composition.dispose();
});
test('service frames require native scope/epoch and exact direction schemas', () => {
  const frame={protocol:1,boot_nonce:'a'.repeat(64),request_id:'r-1',seq:1,kind:'request',method:'goals.read',invocation_ref:'job-1',composition_epoch:1,composition_digest:'b'.repeat(64),package_digest:'c'.repeat(64),deadline_at:2000000000000,payload:{}};
  assert.equal(validateFrame(frame).method,'goals.read');
  for (const change of [{invocation_ref:null},{composition_epoch:null},{composition_epoch:0},{method:'service.invoke'},{payload:{goal_id:'forged'}}]) assert.throws(()=>validateFrame({...frame,...change} as never));
  assert.throws(()=>validateFrame({...frame,kind:'response',payload:{status:'succeeded',value:{},memory_status:'no_learning'}}));
});
test('retained disposed service cannot use a replacement registration as authority', async () => {
  const context = new Context(); context.logger.bufferSize = 0;
  let contacts = 0;
  const client: ScopedRequestClient = {isActive:()=>true,request:async()=>{contacts++;return blocked;}};
  const old = await context.plugin(servicePlugins(client)['seraph.goals.v1'],{});
  const retained = context.seraphGoals;
  await old.dispose();
  const replacement = await context.plugin(servicePlugins(client)['seraph.goals.v1'],{});
  await assert.rejects(retained.read({}),/unavailable/);
  assert.equal(contacts,0);
  assert.deepEqual(await context.seraphGoals.read({}),blocked);
  await replacement.dispose();
});
test('scoped providers return positive read job artifact readback projections without synthesizing outputs', async () => {
  const composition = new Composition(profile()); await composition.start();
  const sha = 'a'.repeat(64);
  const responses = {
    'goals.read': {goal_ref:'goal-1',revision:1,title:'Reviewed goal',description:'Private native projection'},
    'tasks.admit': {job_ref:'child-1',revision:1,state:'accepted',replayed:false},
    'artifacts.adopt': {artifact_ref:'artifact-1',digest:sha,size_bytes:2,receipt_ref:'readback-1'},
    'artifacts.read': {artifact_ref:'artifact-1',digest:sha,size_bytes:2,content:'ok'},
  };
  const calls: string[] = [];
  const client: ScopedRequestClient = {isActive:()=>true,request:async(method)=>{
    calls.push(method);
    assert.ok(Object.hasOwn(responses,method));
    return {status:'succeeded',memory_status:'no_learning',value:responses[method as keyof typeof responses]};
  }};
  const goal = await composition.invoke('goals.read',{},client);
  const job = await composition.invoke('tasks.admit',{request_ref:'native-candidate-1'},client);
  const artifact = await composition.invoke('artifacts.adopt',{request_ref:'native-readback-1'},client);
  const readback = await composition.invoke('artifacts.read',{artifact_ref:'artifact-1',max_bytes:2},client);
  assert.equal(goal.status,'succeeded'); assert.equal(job.status,'succeeded'); assert.equal(artifact.status,'succeeded');
  assert.deepEqual(readback,{status:'succeeded',memory_status:'no_learning',value:responses['artifacts.read']});
  assert.deepEqual(calls,['goals.read','tasks.admit','artifacts.adopt','artifacts.read']);
  await composition.dispose();
});
