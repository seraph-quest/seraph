import {test} from "node:test";
import assert from "node:assert/strict";
import {sourceRevision,sha,deletionOnly} from "./protocol.js";

test("durable source identity never commits to removed text or full URL",async()=>{
  const identity={origin:"https://docs.example",path:"/page",document_id:"same-document",captured_at:1791100104};
  const first={...identity,url:"https://first:0421@docs.example/page?token=1234#private",text:"temporary code 0421"};
  const second={...identity,url:"https://second:9999@docs.example/page?token=9999#different",text:"temporary code 9876"};
  const reviewed="temporary code";
  assert(deletionOnly(first.text,reviewed));assert(deletionOnly(second.text,reviewed));
  assert.equal(await sourceRevision(first),await sourceRevision(second));
  assert.equal(await sourceRevision(first),await sha(JSON.stringify(["seraph.selected-context.source.v1",identity.origin,identity.path,identity.document_id,0,identity.captured_at])));
  assert.notEqual(await sourceRevision(first),await sha(JSON.stringify([first.document_id,first.url,first.captured_at,first.text])));
  assert.notEqual(await sha(reviewed),await sha(reviewed+" 0421"));
});
