// Peer-reflexive discovery can relabel the other endpoint's TURN candidate.
// Match the actual selected endpoints as well as the native candidate types.
import {SocketAddress} from 'node:net';
const candidateTypes=new Set(['host','srflx','prflx','relay']);
function endpoint(address,port,transport){
  const socket=new SocketAddress({address,port:Number(port),family:address.includes(':')?'ipv6':'ipv4'});
  return `${String(transport).toLowerCase()}:[${socket.address}]:${socket.port}`;
}
export function rememberRelay(endpoints,candidate){
  const parts=candidate.trim().split(/\s+/);
  if(parts.length<8||parts[6]!=='typ'||parts[7]!=='relay')return;
  const port=Number(parts[5]);
  if(!Number.isInteger(port)||port<1||port>65535)throw Error('Invalid relay candidate port');
  if(endpoints.size>=128)throw Error('Too many relay endpoints');
  endpoints.add(endpoint(parts[4],port,parts[2]));
}
export function selectedRoute(pair,localRelays,remoteRelays){
  if(!pair||!candidateTypes.has(pair.local?.type)||!candidateTypes.has(pair.remote?.type))
    throw Error('Selected ICE pair unavailable');
  const local=pair.local,remote=pair.remote;
  return local.type==='relay'||remote.type==='relay'
    ||localRelays.has(endpoint(local.address,local.port,local.transportType))
    ||remoteRelays.has(endpoint(remote.address,remote.port,remote.transportType))?'relay':'direct';
}
