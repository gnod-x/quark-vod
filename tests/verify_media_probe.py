#!/usr/bin/env python3
"""Current image, generated one-second media; optional Intel VAAPI, no network/libraries."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',required=True)
    ap.add_argument('--hardware', action='store_true', help='also test Intel renderD128 encode/decode')
    args=ap.parse_args()
    def run(cmd,timeout=30):
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout)
        if r.returncode:raise RuntimeError('media fixture command failed, exit '+str(r.returncode))
        return r.stdout
    image=run(['docker','inspect','-f','{{.Image}}','jellyfin']).strip()
    wrapper='/home/dong/projects/hometheater/custom-ffmpeg'
    name='cinema-probe-'+uuid.uuid4().hex[:10]
    cases=[]
    with tempfile.TemporaryDirectory(prefix='cinema-media-') as tmp:
        os.chmod(tmp,0o755)
        try:
            device=[]
            if args.hardware:
                device=['--device','/dev/dri/renderD128','--group-add',str(os.stat('/dev/dri/renderD128').st_gid)]
            run(['docker','run','-d','--name',name,'--network','none','--read-only','--memory','256m','--cpus','1','--user',str(os.getuid())+':'+str(os.getgid()),*device,'--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/tmp:rw,noexec,nosuid,size=16m','-v',tmp+':/fixture:rw','-v',wrapper+':/custom-ffmpeg:ro','--entrypoint','/bin/sh',image,'-c','sleep 120'])
            ff=['docker','exec',name,'/usr/lib/jellyfin-ffmpeg/ffmpeg']
            run(ff+['-v','error','-f','lavfi','-i','color=c=black:s=64x64:r=2','-f','lavfi','-i','sine=frequency=440:sample_rate=8000','-t','1','-c:v','mpeg4','-c:a','aac','-threads','1','/fixture/sample.mkv'])
            def probe(path,additional=None):
                t=time.monotonic()
                raw=run(['docker','exec','-e','FFPROBE_CACHE_DIR=/fixture/cache',name,'/custom-ffmpeg/ffprobe','-v','error','-show_streams','-show_format','-print_format','json',*(additional or []),'-i',path])
                data=json.loads(raw)
                assert {x['codec_type'] for x in data['streams']} >= {'video','audio'}
                return round(time.monotonic()-t,3)
            cases.append({'name':'generated_mkv_cold','seconds':probe('/fixture/sample.mkv'),'pass':True})
            cases.append({'name':'generated_mkv_cache','seconds':probe('/fixture/sample.mkv'),'pass':True})
            # Invalid media must not produce a reusable positive cache.
            Path(tmp,'invalid.bin').write_bytes(b'not a media stream')
            r=run(['docker','exec','-e','FFPROBE_CACHE_DIR=/fixture/cache',name,'/custom-ffmpeg/ffprobe','-v','error','-show_streams','-print_format','json','-i','/fixture/invalid.bin'])
            json.loads(r)
            cached=list(Path(tmp,'cache').glob('*.json'))
            assert len(cached)==1
            cases.append({'name':'invalid_media_no_cache','pass':True})
            run(['docker','exec',name,'/custom-ffmpeg/ffmpeg','-v','error','-i','/fixture/sample.mkv','-t','0.5','-f','null','-'])
            cases.append({'name':'wrapper_decodes_generated_media','pass':True})
            if args.hardware:
                run(ff+['-v','error','-vaapi_device','/dev/dri/renderD128','-f','lavfi','-i','testsrc2=size=128x128:rate=10','-t','1','-vf','format=nv12,hwupload','-c:v','h264_vaapi','/fixture/hardware.mp4'])
                cases.append({'name':'intel_vaapi_h264_encode','pass':True})
                run(ff+['-v','error','-hwaccel','vaapi','-hwaccel_device','/dev/dri/renderD128','-hwaccel_output_format','vaapi','-i','/fixture/hardware.mp4','-vf','hwdownload,format=nv12','-f','null','-'])
                cases.append({'name':'intel_vaapi_h264_decode','pass':True})
        finally:
            subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=10)
    Path(args.output).write_text(json.dumps({'time':datetime.datetime.now().astimezone().isoformat(),'image':image,'cases':cases,'all_passed':True},indent=2)+'\n')
    print('PASS',len(cases),'isolated real FFmpeg/ffprobe cases')


if __name__=='__main__':main()
