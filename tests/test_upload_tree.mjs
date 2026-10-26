import test from 'node:test';
import assert from 'node:assert/strict';
import {captureDroppedItems, collectDroppedItems} from '../app/static/upload-tree.mjs';

const file = name => ({name,isFile:true,file:resolve=>resolve({name,size:3})});
function directory(name, children, batchSize=100) {
  return {name,isDirectory:true,createReader(){
    let offset=0;
    return {readEntries(resolve){const batch=children.slice(offset,offset+batchSize);offset+=batch.length;resolve(batch);}};
  }};
}

test('captures every root while drag data remains readable', async()=>{
  let readable=true;
  const root=directory('项目',[file('.env')]);
  const items=[{kind:'file',webkitGetAsEntry(){assert.ok(readable);return root;}}];
  const captured=captureDroppedItems({items});readable=false;
  const result=await collectDroppedItems(captured);
  assert.equal(result.files[0].relativePath,'项目/.env');
});

test('reads every batch beyond 100 entries and retains hidden names and empty folders',async()=>{
  const children=Array.from({length:205},(_,i)=>file(`file-${i}.txt`));
  children.push(file('.env'),directory('.config',[file('settings.json')]),directory('空目录',[]));
  const result=await collectDroppedItems([{entry:directory('资料',children)}]);
  assert.equal(result.files.length,207);
  assert.ok(result.files.some(f=>f.relativePath==='资料/file-204.txt'));
  assert.ok(result.files.some(f=>f.relativePath==='资料/.env'));
  assert.ok(result.files.some(f=>f.relativePath==='资料/.config/settings.json'));
  assert.deepEqual(result.directories,['资料','资料/.config','资料/空目录']);
});

test('mixed files and multiple directories preserve independent roots',async()=>{
  const result=await collectDroppedItems([{entry:directory('甲',[file('same.txt')])},{entry:directory('乙',[file('same.txt')])},{file:{name:'.hidden',size:1}}]);
  assert.deepEqual(result.files.map(f=>f.relativePath),['甲/same.txt','乙/same.txt','.hidden']);
});

test('accepts an entirely empty directory',async()=>{
  assert.deepEqual(await collectDroppedItems([{entry:directory('空目录',[])}]),{files:[],directories:['空目录']});
});

test('does not silently skip unreadable entries',async()=>{
  const bad={name:'.private',isFile:true,file:(resolve,reject)=>reject(Error('permission denied'))};
  await assert.rejects(collectDroppedItems([{entry:directory('root',[bad])}]),/permission denied/);
});

test('cancels during recursive enumeration before collecting more files',async()=>{
  let cancelled=false;
  await assert.rejects(collectDroppedItems([{entry:directory('root',[file('a')])}],()=>{if(cancelled)throw Error('cancelled');},()=>cancelled=true),/cancelled/);
});

test('supports unprefixed getAsEntry and falls back for plain files',async()=>{
  const roots=captureDroppedItems({items:[{kind:'string'},{kind:'file',getAsEntry:()=>file('a')},{kind:'file',getAsFile:()=>({name:'b',size:1})}]});
  const result=await collectDroppedItems(roots);
  assert.deepEqual(result.files.map(f=>f.relativePath),['a','b']);
});

test('rejects malformed traversal paths',async()=>{
  for(const name of ['..','bad/name','bad\\name',''])await assert.rejects(collectDroppedItems([{entry:file(name)}]));
});

test('rejects excessive entry counts before upload',async()=>{
  await assert.rejects(collectDroppedItems([{entry:directory('root',Array.from({length:10001},(_,i)=>file(String(i))))}]),/10000/);
});
