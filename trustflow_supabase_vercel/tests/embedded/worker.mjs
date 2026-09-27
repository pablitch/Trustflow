import { PGlite } from '@electric-sql/pglite';
import fs from 'node:fs';
import readline from 'node:readline';
const db = new PGlite({parsers:{1114:x=>x,1184:x=>x},serializers:{1114:x=>x,1184:x=>x}});
await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid primary key,email text);');
await db.exec(fs.readFileSync(process.env.TRUSTFLOW_TEST_SCHEMA, 'utf8'));
console.log(JSON.stringify({ready:true}));
for await (const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})) {
  try {
    const {query,params=[]}=JSON.parse(line);
    let n=0; const q=query.replace(/%s/g,()=>'$'+(++n));
    const result=await db.query(q,params,{rowMode:'array'});
    console.log(JSON.stringify(result));
  } catch(e) { console.log(JSON.stringify({error:e.message})); }
}
await db.close();
