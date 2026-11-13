"""Ed25519 signed release archives. Verification precedes extraction/deployment."""
import argparse
import hashlib
import json
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
    k=sub.add_parser('keygen');k.add_argument('directory')
    for name in ('sign','verify'):
        s=sub.add_parser(name);s.add_argument('archive');s.add_argument('key');s.add_argument('--manifest')
    a=p.parse_args()
    if a.command=='keygen':
        d=Path(a.directory);d.mkdir(parents=True,exist_ok=True)
        if (d/'release-private.pem').exists():raise FileExistsError('Signing key exists')
        key=Ed25519PrivateKey.generate();f=d/'release-private.pem';f.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));f.chmod(0o600)
        (d/'release-public.pem').write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo));return
    archive=Path(a.archive);m=Path(a.manifest or str(archive)+'.manifest.json');sig=Path(str(m)+'.sig')
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    if a.command=='sign':
        body=json.dumps({'schema_version':1,'archive':archive.name,'sha256':digest},sort_keys=True).encode();m.write_bytes(body)
        key=serialization.load_pem_private_key(Path(a.key).read_bytes(),password=None);sig.write_bytes(key.sign(body))
    else:
        body=m.read_bytes();key=serialization.load_pem_public_key(Path(a.key).read_bytes());key.verify(sig.read_bytes(),body)
        if json.loads(body)['sha256']!=digest:raise ValueError('Archive digest mismatch')
        print('Signature and archive digest verified')
if __name__=='__main__':main()
