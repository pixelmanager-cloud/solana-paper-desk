"""Content-addressed historical pages, stored separately from dashboard summaries."""
import json,sqlite3,zlib
from pathlib import Path
from contextlib import closing
from .model import canonical,digest

class EvidenceStore:
    def __init__(self,path,max_bytes=256*1024*1024,*,read_only=False):
        self.path=Path(path);self.max_bytes=max_bytes;self.read_only=read_only
        if read_only:
            if not self.path.is_file():raise ValueError("Evidence database missing")
            return
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as c:
            c.execute('CREATE TABLE IF NOT EXISTS pages(hash TEXT PRIMARY KEY,payload BLOB NOT NULL,raw_bytes INTEGER NOT NULL)')
    def connect(self):
        target=self.path.resolve().as_uri()+"?mode=ro" if self.read_only else self.path
        return sqlite3.connect(target,uri=self.read_only,timeout=20,isolation_level=None)
    def save(self,payload):
        if self.read_only:raise ValueError("Evidence store is read-only")
        raw=canonical(payload).encode()
        if len(raw)>16*1024*1024:raise ValueError('Evidence page exceeds size budget')
        key=digest(payload);compressed=zlib.compress(raw)
        with closing(self.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                if not c.execute('SELECT 1 FROM pages WHERE hash=?',(key,)).fetchone():
                    used=c.execute('SELECT COALESCE(SUM(length(payload)),0) FROM pages').fetchone()[0]
                    if used+len(compressed)>self.max_bytes:raise ValueError('Evidence storage budget exhausted')
                    c.execute('INSERT INTO pages VALUES(?,?,?)',(key,compressed,len(raw)))
                c.execute('COMMIT')
            except BaseException:
                c.execute('ROLLBACK');raise
        return key
    def load(self,key):
        with closing(self.connect()) as c:row=c.execute('SELECT payload,raw_bytes FROM pages WHERE hash=?',(key,)).fetchone()
        if not row or not 0<=row[1]<=16*1024*1024:raise ValueError('Evidence missing or oversized')
        inflater=zlib.decompressobj();raw=inflater.decompress(row[0],row[1]+1)
        if not inflater.eof or inflater.unused_data or len(raw)!=row[1]:raise ValueError('Evidence encoding mismatch')
        payload=json.loads(raw)
        if digest(payload)!=key:raise ValueError('Evidence checksum mismatch')
        return payload
