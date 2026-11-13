import subprocess
import sys
def test_signed_artifact_rejects_tampering(tmp_path):
    def call(*args,ok=True):
        r=subprocess.run([sys.executable,'-m','soarm.release',*map(str,args)],capture_output=True)
        assert (r.returncode==0)==ok,r.stderr
    call('keygen',tmp_path/'keys');archive=tmp_path/'release.tar.gz';archive.write_bytes(b'release')
    call('sign',archive,tmp_path/'keys/release-private.pem');call('verify',archive,tmp_path/'keys/release-public.pem')
    archive.write_bytes(b'tampered');call('verify',archive,tmp_path/'keys/release-public.pem',ok=False)
