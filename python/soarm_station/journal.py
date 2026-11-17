"""Durable schedule, journal and exact-key outbox acknowledgements."""
import json
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

def key(s):return (s['device_id'],s['runtime_boot_id'],s['sequence'])
class Journal:
    MAINTENANCE_BATCH=10000
    DAY_NS=86400*10**9
    MAX_NS=2**63-1

    def __init__(self,path,limit_mb=2048,*,retention_days=None,preserve_unacked=True,event_limit=10000):
        if isinstance(limit_mb,bool) or not isinstance(limit_mb,(int,float)) or not math.isfinite(limit_mb) or limit_mb<=0:
            raise ValueError('Journal limit_mb must be positive and finite')
        if retention_days is not None and (isinstance(retention_days,bool) or not isinstance(retention_days,(int,float))
                or not math.isfinite(retention_days) or retention_days<=0):
            raise ValueError('Journal retention_days must be positive and finite or None')
        if type(preserve_unacked)!=bool:raise ValueError('preserve_unacked must be boolean')
        if type(event_limit)!=int or event_limit<1:raise ValueError('event_limit must be a positive integer')
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.path=Path(path);self.limit_mb=limit_mb;self.lock=threading.RLock()
        self.retention_days=retention_days;self.preserve_unacked=preserve_unacked;self.event_limit=event_limit
        self.db=sqlite3.connect(path,timeout=2,check_same_thread=False)
        try:
            self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
            # Bound SQLite's connection cache and normal WAL reuse, not record age.
            # A reader can pin WAL frames; quota checks also count that physical WAL.
            self.db.execute('PRAGMA cache_size=-2048');self.db.execute('PRAGMA mmap_size=0')
            self.db.execute('PRAGMA wal_autocheckpoint=256');self.db.execute('PRAGMA journal_size_limit=1048576')
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS samples(sequence INTEGER PRIMARY KEY AUTOINCREMENT,device_id TEXT NOT NULL,boot_id TEXT NOT NULL,scheduled_ns INTEGER NOT NULL,arm_id TEXT NOT NULL,payload TEXT NOT NULL,acked INTEGER NOT NULL DEFAULT 0,UNIQUE(device_id,boot_id,sequence));
            CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL,recorded_ns INTEGER);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS pending ON samples(acked,sequence);
            CREATE INDEX IF NOT EXISTS samples_age ON samples(scheduled_ns,sequence);
            """)
            columns={row[1] for row in self.db.execute('PRAGMA table_info(events)')}
            if 'recorded_ns' not in columns:
                self.db.execute('ALTER TABLE events ADD COLUMN recorded_ns INTEGER')
                self._migrate_event_times()
            self.db.execute('CREATE INDEX IF NOT EXISTS events_age ON events(recorded_ns)')
            self.db.commit()
        except BaseException:
            try:self.db.close()
            except Exception:pass
            raise

    @classmethod
    def _event_time(cls,event,fallback=None):
        if not isinstance(event,dict):return fallback
        value=event.get('time_ns')
        if type(value)==int and 0<value<=cls.MAX_NS:return value
        stamp=event.get('time')
        if isinstance(stamp,dict):
            sec,nanosec=stamp.get('sec'),stamp.get('nanosec',0)
            if type(sec)==int and type(nanosec)==int and sec>=0 and 0<=nanosec<10**9:
                value=sec*10**9+nanosec
                if 0<value<=cls.MAX_NS:return value
        return fallback

    def _migrate_event_times(self):
        # Existing unknown timestamps remain NULL and cannot be aged out. This
        # migration records evidence already in the payload; it never prunes.
        after=0
        while True:
            rows=self.db.execute('SELECT rowid,payload FROM events WHERE rowid>? ORDER BY rowid LIMIT 256',(after,)).fetchall()
            if not rows:return
            for rowid,payload in rows:
                try:stamp=self._event_time(json.loads(payload))
                except (ValueError,TypeError):stamp=None
                if stamp is not None:self.db.execute('UPDATE events SET recorded_ns=? WHERE rowid=?',(stamp,rowid))
            after=rows[-1][0]

    def storage(self):
        """Physical files plus logical growth that a checkpoint must materialize."""
        with self.lock:
            sizes=[]
            for suffix in ('','-wal','-shm'):
                try:sizes.append(Path(str(self.path)+suffix).stat().st_size)
                except FileNotFoundError:sizes.append(0)
            pages=self.db.execute('PRAGMA page_count').fetchone()[0]
            page_size=self.db.execute('PRAGMA page_size').fetchone()[0]
            logical=pages*page_size;total=sum(sizes);limit=int(self.limit_mb*1024*1024)
            accounted=max(sizes[0],logical)+sizes[1]+sizes[2]
            return {'database_bytes':sizes[0],'wal_bytes':sizes[1],'shm_bytes':sizes[2],
                'total_bytes':total,'logical_database_bytes':logical,'accounted_bytes':accounted,
                'limit_bytes':limit,'over_limit':accounted>=limit,
                'free_pages':self.db.execute('PRAGMA freelist_count').fetchone()[0]}

    def _reserve(self,payload_bytes,rows=1):
        # Conservative room for payload overflow pages plus table/index splits,
        # sequence/meta updates and WAL frame headers before a commit. No full
        # payload/database copy or post-commit failure/retry ambiguity is needed.
        page_size=self.db.execute('PRAGMA page_size').fetchone()[0]
        pages=math.ceil(payload_bytes/page_size)+32*max(1,rows)
        return pages*(page_size+24)+32768

    def _check_quota(self,reserve):
        usage=self.storage()
        if usage['accounted_bytes']+reserve>usage['limit_bytes']:
            raise OSError('Telemetry storage limit reached; recent and unacknowledged data retained')

    def _checkpoint(self):
        # PASSIVE copies only immediately available frames. TRUNCATE is attempted
        # only after every frame is checkpointed, with no wait for a reader lock.
        timeout=self.db.execute('PRAGMA busy_timeout').fetchone()[0]
        try:
            self.db.execute('PRAGMA busy_timeout=0')
            row=self.db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
            result=dict(zip(('busy','log_frames','checkpointed_frames'),row))
            result.update(truncated=False,truncate_busy=None)
            if row[0]==0 and row[1]==row[2]:
                truncate=self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                result['truncate_busy']=truncate[0];result['truncated']=truncate[0]==0
            return result
        finally:self.db.execute(f'PRAGMA busy_timeout={int(timeout)}')

    @contextmanager
    def _write(self,payload_bytes=0,rows=1):
        with self.lock:
            reserve=self._reserve(payload_bytes,rows)
            try:self._check_quota(reserve)
            except OSError:
                self._checkpoint()
                self._check_quota(reserve)
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self._check_quota(reserve)
                yield
                self._check_quota(reserve)
                self.db.commit()
            except BaseException:
                self.db.rollback();raise

    def _prepare_event(self,event):
        if not isinstance(event,dict):raise ValueError('Event must be a mapping')
        event=dict(event);event.setdefault('event_id',uuid.uuid4().hex)
        if not isinstance(event['event_id'],str) or not event['event_id']:raise ValueError('Event ID is required')
        payload=json.dumps(event,allow_nan=False)
        return event,payload,self._event_time(event,time.time_ns())

    def _insert_event(self,prepared):
        event,payload,stamp=prepared
        if self.db.execute('SELECT 1 FROM events WHERE event_id=?',(event['event_id'],)).fetchone():return False
        if self.db.execute('SELECT count(*) FROM events').fetchone()[0]>=self.event_limit:
            raise OSError('Telemetry event limit reached; retained events were not deleted')
        self.db.execute('INSERT INTO events(event_id,payload,recorded_ns) VALUES(?,?,?)',(event['event_id'],payload,stamp))
        return True
    def meta(self,k,default=None):
        with self.lock:
            row=self.db.execute('SELECT value FROM meta WHERE key=?',(k,)).fetchone()
            return json.loads(row[0]) if row else default
    def set_meta(self,k,v):
        encoded=json.dumps(v,allow_nan=False)
        with self.lock:
            existing=self.db.execute('SELECT value FROM meta WHERE key=?',(k,)).fetchone()
            if existing and existing[0]==encoded:return
            with self._write(len(encoded.encode())):self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(k,encoded))

    def schedule_info(self):
        with self.lock:
            next_ns=self.meta('next_ns');period_ns=self.meta('period_ns')
            latest=self.db.execute('SELECT DISTINCT scheduled_ns FROM samples ORDER BY scheduled_ns DESC LIMIT 2').fetchall()
            if period_ns is None and len(latest)==2:
                difference=latest[0][0]-latest[1][0]
                if difference>0:period_ns=difference
            return {'next_ns':next_ns,'period_ns':period_ns,'has_samples':bool(latest)}

    def configure_schedule(self,period_ns,next_ns,event=None):
        if type(period_ns)!=int or not 0<period_ns<=self.MAX_NS:raise ValueError('period_ns must be a positive integer')
        if type(next_ns)!=int or not 0<=next_ns<=self.MAX_NS:raise ValueError('next_ns must be a nonnegative timestamp')
        prepared=self._prepare_event(event) if event is not None else None
        size=len(prepared[1].encode()) if prepared else 0
        with self.lock:
            if prepared is None and self.meta('period_ns')==period_ns and self.meta('next_ns')==next_ns:
                return {'period_ns':period_ns,'next_ns':next_ns,'event':None}
            with self._write(size,3 if prepared else 2):
                self.db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',
                    [('period_ns',json.dumps(period_ns)),('next_ns',json.dumps(next_ns))])
                if prepared:self._insert_event(prepared)
        return {'period_ns':period_ns,'next_ns':next_ns,'event':prepared[0] if prepared else None}
    def append(self,samples,next_ns):
        samples=list(samples)
        size=sum(len(json.dumps(sample,allow_nan=False).encode())+32 for sample in samples)
        with self._write(size,len(samples)+1):
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
        # Exact-key acknowledgements are idempotent. Smaller committed batches
        # allow cleanup near quota without an unbounded WAL exception; a later
        # retry safely handles any still-unacknowledged remainder.
        pending=list(dict.fromkeys(key(ack) for ack in acks))
        with self.lock:
            pending=[item for item in pending if self.db.execute(
                'SELECT 1 FROM samples WHERE device_id=? AND boot_id=? AND sequence=? AND acked=0',item).fetchone()]
            offset=0;batch_size=min(128,len(pending))
            while offset<len(pending):
                batch=pending[offset:offset+batch_size]
                try:
                    with self._write(rows=len(batch)):
                        self.db.executemany('UPDATE samples SET acked=1 WHERE device_id=? AND boot_id=? AND sequence=? AND acked=0',batch)
                except OSError:
                    if batch_size==1:raise
                    batch_size=max(1,batch_size//2)
                else:offset+=len(batch)
    def history(self,arm='',after=0,limit=100,start_ns=0,end_ns=9223372036854775807):
        limit=max(1,min(500,limit))
        with self.lock:
            rows=self.db.execute('SELECT payload,acked FROM samples WHERE sequence>? AND scheduled_ns BETWEEN ? AND ? AND (?="" OR arm_id=?) ORDER BY sequence LIMIT ?',
                (after,start_ns,end_ns,arm,arm,limit+1)).fetchall()
            return [{'sample':json.loads(r[0]),'remote_committed':bool(r[1])} for r in rows[:limit]],len(rows)>limit
    def event(self,e):
        prepared=self._prepare_event(e)
        with self.lock:
            if self.db.execute('SELECT 1 FROM events WHERE event_id=?',(prepared[0]['event_id'],)).fetchone():return prepared[0]
            with self._write(len(prepared[1].encode())):self._insert_event(prepared)
        return prepared[0]
    def events(self,limit=100):
        with self.lock:return [json.loads(x[0]) for x in self.db.execute('SELECT payload FROM events ORDER BY rowid DESC LIMIT ?',(min(limit,100),))]
    def counts(self):
        with self.lock:
            total,acked=self.db.execute('SELECT count(*),coalesce(sum(acked),0) FROM samples').fetchone()
            return {'local_committed':total,'remote_committed':acked,'queued':total-acked}

    def maintain(self,now_ns):
        if type(now_ns)!=int or not 0<=now_ns<=self.MAX_NS:raise ValueError('now_ns must be a nonnegative timestamp')
        cutoff=None if self.retention_days is None else now_ns-int(self.retention_days*self.DAY_NS)
        deleted_samples=deleted_events=0
        more_samples=more_events=False
        with self.lock:
            if cutoff is not None:
                eligible='scheduled_ns<?'+(' AND acked=1' if self.preserve_unacked else '')
                with self.db:
                    deleted_samples=self.db.execute(
                        'DELETE FROM samples WHERE sequence IN (SELECT sequence FROM samples WHERE '+eligible+
                        ' ORDER BY scheduled_ns,sequence LIMIT ?)',(cutoff,self.MAINTENANCE_BATCH)).rowcount
                    deleted_events=self.db.execute(
                        'DELETE FROM events WHERE rowid IN (SELECT rowid FROM events WHERE recorded_ns<? '
                        'ORDER BY recorded_ns,rowid LIMIT ?)',(cutoff,self.MAINTENANCE_BATCH)).rowcount
                more_samples=self.db.execute('SELECT 1 FROM samples WHERE '+eligible+' LIMIT 1',(cutoff,)).fetchone() is not None
                more_events=self.db.execute('SELECT 1 FROM events WHERE recorded_ns<? LIMIT 1',(cutoff,)).fetchone() is not None
            checkpoint=self._checkpoint()
            events=self.db.execute('SELECT count(*) FROM events').fetchone()[0]
            return {'cutoff_ns':cutoff,'deleted_samples':deleted_samples,'deleted_events':deleted_events,
                'more_expired_samples':more_samples,'more_expired_events':more_events,'counts':self.counts(),
                'events':events,'event_limit':self.event_limit,'event_limit_reached':events>=self.event_limit,
                'storage':self.storage(),'checkpoint':checkpoint}

    def close(self):
        with self.lock:self.db.close()
