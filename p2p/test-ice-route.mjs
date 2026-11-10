import {test} from 'node:test';
import assert from 'node:assert/strict';
import {rememberRelay,selectedRoute} from './ice_route.mjs';
const peer=(type,address='203.0.113.1',port=5000)=>({type,address,port,transportType:'Udp'});
test('ordinary host and reflexive candidate pairs remain direct',()=>{
  assert.equal(selectedRoute({local:peer('srflx'),remote:peer('prflx')},new Set(),new Set()),'direct');
});
test('a TURN endpoint rediscovered as peer-reflexive remains relay',()=>{
  const remote=new Set();rememberRelay(remote,'candidate:1 1 UDP 123 203.0.113.2 5001 typ relay raddr 0.0.0.0 rport 0');
  assert.equal(selectedRoute({local:peer('srflx'),remote:peer('prflx','203.0.113.2',5001)},new Set(),remote),'relay');
  assert.equal(selectedRoute({local:peer('prflx','203.0.113.2',5001),remote:peer('srflx')},remote,new Set()),'relay');
  rememberRelay(remote,'candidate:2 1 UDP 123 2001:0DB8:0:0:0:0:0:1 5001 typ relay');
  assert.equal(selectedRoute({local:peer('host'),remote:peer('prflx','2001:db8::1',5001)},new Set(),remote),'relay');
});
test('gathering TURN does not mark a separately selected direct endpoint as relay',()=>{
  const known=new Set();rememberRelay(known,'candidate:1 1 UDP 123 203.0.113.2 5001 typ relay');
  assert.equal(selectedRoute({local:peer('host'),remote:peer('prflx','203.0.113.2',5002)},known,known),'direct');
  assert.equal(selectedRoute({local:peer('relay'),remote:peer('host')},new Set(),new Set()),'relay');
});
test('missing native pair fails instead of reporting an unverified direct connection',()=>{
  assert.throws(()=>selectedRoute(null,new Set(),new Set()),/unavailable/);
});
