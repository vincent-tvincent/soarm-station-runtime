"""Durable schedule, journal and exact-key outbox acknowledgements."""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

def key(s):return (s['device_id'],s['runtime_boot_id'],s['sequence'])
class Journal:
    def __init__(self,path,limit_mb=2048):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.path=path;self.limit_mb=limit_mb;self.lock=threading.RLock()
        self.db=sqlite3.connect(path,timeout=2,check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS samples(sequence INTEGER PRIMARY KEY AUTOINCREMENT,device_id TEXT NOT NULL,boot_id TEXT NOT NULL,scheduled_ns INTEGER NOT NULL,arm_id TEXT NOT NULL,payload TEXT NOT NULL,acked INTEGER NOT NULL DEFAULT 0,UNIQUE(device_id,boot_id,sequence));
        CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS pending ON samples(acked,sequence);
        """)
        self.db.commit()
    def meta(self,k,default=None):
        with self.lock:
            row=self.db.execute('SELECT value FROM meta WHERE key=?',(k,)).fetchone()
            return json.loads(row[0]) if row else default
    def set_meta(self,k,v):
        with self.lock,self.db:self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(k,json.dumps(v)))
    def append(self,samples,next_ns):
        with self.lock,self.db:
            pages=self.db.execute('PRAGMA page_count').fetchone()[0];size=self.db.execute('PRAGMA page_size').fetchone()[0]
            if pages*size>self.limit_mb*1024*1024:raise OSError('Telemetry storage limit reached; unacknowledged data retained')
            result=[]
            for s in samples:
                cursor=self.db.execute('INSERT INTO samples(device_id,boot_id,scheduled_ns,arm_id,payload) VALUES(?,?,?,?,?)',
                    (s['device_id'],s['runtime_boot_id'],s['scheduled_ns'],s['arm_id'],'{}'))
                s={**s,'sequence':cursor.lastrowid}
                self.db.execute('UPDATE samples SET payload=? WHERE sequence=?',(json.dumps(s,allow_nan=False),s['sequence']))
                result.append(s)
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('next_ns',json.dumps(next_ns)))
            return result
    def pending(self,limit=128):
        with self.lock:return [json.loads(x[0]) for x in self.db.execute('SELECT payload FROM samples WHERE acked=0 ORDER BY sequence LIMIT ?',(min(limit,128),))]
    def acknowledge(self,acks):
        with self.lock,self.db:
            for a in acks:
                self.db.execute('UPDATE samples SET acked=1 WHERE device_id=? AND boot_id=? AND sequence=?',
                    (a['device_id'],a['runtime_boot_id'],a['sequence']))
    def history(self,arm='',after=0,limit=100,start_ns=0,end_ns=9223372036854775807):
        limit=max(1,min(500,limit))
        with self.lock:
            rows=self.db.execute('SELECT payload,acked FROM samples WHERE sequence>? AND scheduled_ns BETWEEN ? AND ? AND (?="" OR arm_id=?) ORDER BY sequence LIMIT ?',
                (after,start_ns,end_ns,arm,arm,limit+1)).fetchall()
            return [{'sample':json.loads(r[0]),'remote_committed':bool(r[1])} for r in rows[:limit]],len(rows)>limit
    def event(self,e):
        e={**e};e.setdefault('event_id',uuid.uuid4().hex)
        with self.lock,self.db:
            self.db.execute('INSERT OR IGNORE INTO events VALUES(?,?)',(e['event_id'],json.dumps(e,allow_nan=False)))
        return e
    def events(self,limit=100):
        with self.lock:return [json.loads(x[0]) for x in self.db.execute('SELECT payload FROM events ORDER BY rowid DESC LIMIT ?',(min(limit,100),))]
    def counts(self):
        with self.lock:
            total,acked=self.db.execute('SELECT count(*),coalesce(sum(acked),0) FROM samples').fetchone()
            return {'local_committed':total,'remote_committed':acked,'queued':total-acked}
    def close(self):self.db.close()
