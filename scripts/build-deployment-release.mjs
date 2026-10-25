import {mkdir,readFile,writeFile,copyFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {loadRecipe} from '../packages/dsh-matpilot/deployment/engine.mjs';
const recipe=await loadRecipe();
const {version}=JSON.parse(await readFile('packages/dsh-matpilot/package.json','utf8'));
if(!/^[0-9A-Za-z.-]+$/.test(version))throw Error('Invalid release version');
const dir=`releases/harness/${version}`;await mkdir(dir,{recursive:true});
const args=['pack','./packages/dsh-matpilot','--pack-destination',dir,'--json'];
const result=process.platform==='win32'?spawnSync('cmd.exe',['/d','/s','/c',`npm ${args.join(' ')}`],{encoding:'utf8',windowsHide:true}):spawnSync('npm',args,{encoding:'utf8'});
if(result.status)throw Error(result.stderr);
const metadata=JSON.parse(result.stdout)[0];
for(const file of [recipe.core_wheel,recipe.mattergen_wheel,'MatterGen-LICENSE.txt'])if(!metadata.files.some(f=>f.path===`deployment/assets/${file}`))throw Error('Required bundled asset missing: '+file);
for(const f of metadata.files)if(/node_modules|\.log$|\.partial|\.rejected|\.work\//.test(f.path))throw Error('Unexpected package file '+f.path);
const tgz=metadata.filename;await copyFile(`packages/dsh-matpilot/deployment/assets/${recipe.core_wheel}`,`${dir}/${recipe.core_wheel}`);
await copyFile('docs/deployment.md',`${dir}/INSTALL.md`);
await copyFile('docs/deployment-acceptance.md',`${dir}/VALIDATION.md`);
await copyFile('benchmarks/deployment/protocol.md',`${dir}/COMPARISON.md`);
const names=[tgz,recipe.core_wheel,'INSTALL.md','VALIDATION.md','COMPARISON.md'];
const contents=await Promise.all(names.map(async name=>({name,data:await readFile(`${dir}/${name}`)})));
const sha=data=>createHash('sha256').update(data).digest('hex');
const inner=contents.map(f=>`${sha(f.data)}  ${f.name}`).join('\n')+'\n';
contents.push({name:'SHA256SUMS.txt',data:Buffer.from(inner)});
// Portable, deterministic ZIP_STORED archive; inputs are already compressed wheels/tgz.
const crcTable=Array.from({length:256},(_,n)=>{for(let k=0;k<8;k++)n=(n&1)?0xedb88320^(n>>>1):n>>>1;return n>>>0;});
function crc32(data){let n=0xffffffff;for(const b of data)n=crcTable[(n^b)&255]^(n>>>8);return(n^0xffffffff)>>>0;}
const chunks=[],central=[];let offset=0;
for(const {name,data} of contents){
 const bytes=Buffer.from(name),crc=crc32(data),header=Buffer.alloc(30);
 header.writeUInt32LE(0x04034b50);header.writeUInt16LE(20,4);header.writeUInt16LE(0x800,6);header.writeUInt16LE(33,12);header.writeUInt32LE(crc,14);header.writeUInt32LE(data.length,18);header.writeUInt32LE(data.length,22);header.writeUInt16LE(bytes.length,26);
 const entry=Buffer.alloc(46);entry.writeUInt32LE(0x02014b50);entry.writeUInt16LE(20,4);entry.writeUInt16LE(20,6);entry.writeUInt16LE(0x800,8);entry.writeUInt16LE(33,14);entry.writeUInt32LE(crc,16);entry.writeUInt32LE(data.length,20);entry.writeUInt32LE(data.length,24);entry.writeUInt16LE(bytes.length,28);entry.writeUInt32LE(offset,42);
 chunks.push(header,bytes,data);central.push(entry,bytes);offset+=header.length+bytes.length+data.length;
}
const directory=Buffer.concat(central),end=Buffer.alloc(22);end.writeUInt32LE(0x06054b50);end.writeUInt16LE(contents.length,8);end.writeUInt16LE(contents.length,10);end.writeUInt32LE(directory.length,12);end.writeUInt32LE(offset,16);
const zip=Buffer.concat([...chunks,directory,end]),zipName=`MatPilot-setup-${version}.zip`;await writeFile(`${dir}/${zipName}`,zip);
await writeFile(`${dir}/SHA256SUMS.txt`,inner+`${sha(zip)}  ${zipName}\n`);
await writeFile(`${dir}/package-manifest.json`,JSON.stringify({version,recipe_id:recipe.id,files:metadata.files.map(f=>({path:f.path,size:f.size})),sha256:sha(await readFile(`${dir}/${tgz}`))},null,2)+'\n');
console.log(JSON.stringify({directory:dir,package:tgz,zip:zipName,files:metadata.files.length,bytes:zip.length},null,2));
