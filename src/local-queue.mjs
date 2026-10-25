import {DatabaseSync} from 'node:sqlite';
import {chmodSync} from 'node:fs';
import {operate, TERMINAL} from './call-queue.mjs';

// One local process owns the database. Transactions also prevent a diagnostic
// client from observing a partially committed claim or cancellation.
export function openLocalQueue(path) {
  const db = new DatabaseSync(path);
  if (path !== ':memory:') chmodSync(path,0o600);
  db.exec('PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)');
  db.exec('CREATE TABLE IF NOT EXISTS registered (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)');
  const read=db.prepare('SELECT value FROM state WHERE id=1');
  const write=db.prepare('INSERT INTO state(id,value) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value');
  const transaction=fn=>{
    db.exec('BEGIN IMMEDIATE');
    try {
      const row=read.get(), state=row?JSON.parse(row.value):{};
      const result=fn(state);
      write.run(JSON.stringify(state));db.exec('COMMIT');return result;
    } catch(error) {db.exec('ROLLBACK');throw error;}
  };
  transaction(state=>{
    // Never replay a queued or claimed call after process recovery. The helper
    // loses its heartbeat and closes its existing phone/browser session.
    state.mac=null;
    for(const job of state.jobs??[]) if(!TERMINAL.has(job.status)) {
      job.status='failed';job.code='LOCAL_BACKEND_RESTARTED';
      job.cancel_requested=true;job.updated_at=Date.now();
    }
  });
  return {perform:(action,input={})=>transaction(state=>{
    const result=operate(state,action,input);
    if(action==='pulse'&&input.busy&&!result.cancel_call_id) {
      const interrupted=state.jobs.find(job=>job.instance===input.instance&&job.code==='LOCAL_BACKEND_RESTARTED');
      if(interrupted) return {...result,cancel_call_id:interrupted.id};
    }
    return result;
  }),
    readRegistered:()=>JSON.parse(db.prepare('SELECT value FROM registered WHERE id=1').get()?.value??'null'),
    writeRegistered:value=>db.prepare('INSERT INTO registered(id,value) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value').run(JSON.stringify(value)),
    close:()=>db.close()};
}
