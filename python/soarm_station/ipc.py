"""Length-prefixed bounded local IPC; filesystem permissions authenticate the peer."""
import json
import socket
import struct
import threading
from .policy_common import PolicyError
MAX_BYTES=1048576
def receive(sock):
    def exact(n):
        result=bytearray()
        while len(result)<n:
            b=sock.recv(n-len(result))
            if not b: raise EOFError()
            result.extend(b)
        return bytes(result)
    n=struct.unpack('!I',exact(4))[0]
    if n>MAX_BYTES: raise ValueError('IPC message too large')
    return json.loads(exact(n))
def send(sock,value):
    b=json.dumps(value,allow_nan=False,separators=(',',':')).encode()
    if len(b)>MAX_BYTES: raise ValueError('IPC response too large')
    sock.sendall(struct.pack('!I',len(b))+b)
class Client:
    def __init__(self,path,timeout_s=2): self.path=path;self.timeout_s=timeout_s;self.sock=None;self.lock=threading.Lock()
    def call(self,op,**kwargs):
        with self.lock:
            try:
                if self.sock is None:
                    self.sock=socket.socket(socket.AF_UNIX);self.sock.settimeout(self.timeout_s);self.sock.connect(self.path)
                send(self.sock,{'op':op,**kwargs});r=receive(self.sock)
                if not r['ok']:
                    if r.get('code'):raise PolicyError(r['code'],r['error'],r.get('status',400),r.get('details'))
                    raise ValueError(r['error'])
                return r['data']
            except (OSError,EOFError):
                if self.sock: self.sock.close()
                self.sock=None;raise
    def close(self):
        if self.sock: self.sock.close();self.sock=None
