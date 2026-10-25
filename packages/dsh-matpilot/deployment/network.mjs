import http from 'node:http';
import net from 'node:net';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { systemProxy } from './download.mjs';
import { targetRun } from './platform.mjs';

// WSL NAT cannot reach a Windows loopback-only proxy. Relay only pinned package
// source hosts, only from this WSL guest, with an ephemeral credential. TLS remains
// end-to-end between uv and the source; this code does not decrypt downloads.
export async function withPackageNetwork(target, callback) {
  const configured=await systemProxy();
  if(target.kind!=='wsl' || !configured) return callback({});
  const proxy=new URL(configured);
  if(proxy.protocol!=='http:') return callback({});
  const routes=await targetRun(target,"ip -4 route get 1.1.1.1; ip -4 route show default",[],{timeout:10000});
  const guest=/\bsrc ([0-9.]+)/.exec(routes.stdout)?.[1];
  const gateway=/\bdefault via ([0-9.]+)/.exec(routes.stdout)?.[1];
  if(!guest || !gateway) throw Error('Cannot determine WSL NAT addresses for the configured Windows proxy.');
  const token=randomBytes(24).toString('hex');
  const auth='Basic '+Buffer.from('matpilot:'+token).toString('base64');
  const allow=new Set(['pypi.org','files.pythonhosted.org','download.pytorch.org','data.pyg.org','github.com','objects.githubusercontent.com','release-assets.githubusercontent.com','releases.astral.sh']);
  const sockets=new Set();
  const server=http.createServer((_q,r)=>{r.writeHead(405);r.end();});
  server.on('connection',s=>{sockets.add(s);s.once('close',()=>sockets.delete(s));});
  server.on('connect',(request,client,head)=>{
    const supplied=Buffer.from(request.headers['proxy-authorization']??'');
    const authorized=supplied.length===Buffer.byteLength(auth) && timingSafeEqual(supplied,Buffer.from(auth));
    const peer=client.remoteAddress?.replace(/^::ffff:/,'');
    const match=/^([A-Za-z0-9.-]+):443$/.exec(request.url??'');
    if(!authorized || peer!==guest || !match || !allow.has(match[1].toLowerCase())) {client.end('HTTP/1.1 403 Forbidden\r\n\r\n');return;}
    const upstream=net.connect({host:proxy.hostname,port:Number(proxy.port)||80});sockets.add(upstream);
    upstream.once('close',()=>sockets.delete(upstream));
    const fail=()=>{client.destroy();upstream.destroy();};client.on('error',fail);upstream.on('error',fail);
    const timer=setTimeout(fail,20000);timer.unref();
    upstream.once('connect',()=>{
      let header=`CONNECT ${request.url} HTTP/1.1\r\nHost: ${request.url}\r\n`;
      if(proxy.username) header+='Proxy-Authorization: Basic '+Buffer.from(decodeURIComponent(proxy.username)+':'+decodeURIComponent(proxy.password)).toString('base64')+'\r\n';
      upstream.write(header+'\r\n');
    });
    let buffer=Buffer.alloc(0);
    const response=chunk=>{
      buffer=Buffer.concat([buffer,chunk]);if(buffer.length>32768){clearTimeout(timer);fail();return;}
      const end=buffer.indexOf('\r\n\r\n');if(end<0)return;
      clearTimeout(timer);upstream.off('data',response);
      if(!/^HTTP\/1\.[01] 200\b/.test(buffer.toString('latin1'))){fail();return;}
      client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if(buffer.length>end+4)client.write(buffer.subarray(end+4));if(head.length)upstream.write(head);
      upstream.pipe(client);client.pipe(upstream);
    };
    upstream.on('data',response);
    client.once('close',()=>{clearTimeout(timer);upstream.destroy();});
  });
  await new Promise((ok,bad)=>{server.once('error',bad);server.listen(0,gateway,ok);});
  try {
    const url=`http://matpilot:${token}@${gateway}:${server.address().port}`;
    return await callback({HTTPS_PROXY:url,HTTP_PROXY:url,https_proxy:url,http_proxy:url,NO_PROXY:'localhost,127.0.0.1',no_proxy:'localhost,127.0.0.1'});
  } finally {
    for(const socket of sockets)socket.destroy();await new Promise(r=>server.close(r));
  }
}
