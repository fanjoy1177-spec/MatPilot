import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,readFile,readdir,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import {download} from '../../packages/dsh-matpilot/deployment/download.mjs';
const digest=x=>createHash('sha256').update(x).digest('hex');
async function setup(t){const dir=await mkdtemp(join(tmpdir(),'matpilot-download-'));t.after(()=>rm(dir,{recursive:true,force:true}));return [dir,join(dir,'asset')];}
test('fully downloaded bytes recover from abrupt TLS close only after exact checksum',async t=>{
 const [,dest]=await setup(t);const data='verified test fixture';
 const result=await download('https://example.org/asset',dest,digest(data),undefined,{request:async()=>{await writeFile(dest+'.partial',data);return {code:56,stderr:'abrupt close'};}});
 assert.match(result.transport_warning,/56/);assert.equal(await readFile(dest,'utf8'),data);
});
test('partial download survives transport failure then resumes the same artifact',async t=>{
 const [,dest]=await setup(t);let calls=0;
 const request=async(_cmd,args)=>{calls++;assert.ok(args.includes('--continue-at'));await writeFile(dest+'.partial',calls===1?'part':'part-complete');return {code:calls===1?56:0,stderr:'network'};};
 await assert.rejects(download('https://example.org/asset',dest,digest('part-complete'),undefined,{request}),/partial file kept/);
 assert.equal(await readFile(dest+'.partial','utf8'),'part');
 await download('https://example.org/asset',dest,digest('part-complete'),undefined,{request});assert.equal(calls,2);
});
test('wrong hash cannot enter installation even when transport succeeds',async t=>{
 const [dir,dest]=await setup(t);
 await assert.rejects(download('https://example.org/asset',dest,digest('right'),undefined,{request:async()=>{await writeFile(dest+'.partial','wrong');return {code:0};}}),/SHA256 mismatch/);
 assert.ok((await readdir(dir)).some(x=>x.includes('.rejected-')));await assert.rejects(readFile(dest),{code:'ENOENT'});
});
