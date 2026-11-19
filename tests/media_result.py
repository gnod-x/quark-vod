"""FFmpeg can exit zero after a partial demux; require the requested duration."""
import re


def assess(stdout,stderr,returncode,seconds=12):
    frames=[int(x) for x in re.findall(r'^frame=(\d+)$',stdout,re.M)]
    times=[int(x) for x in re.findall(r'^out_time_us=(\d+)$',stdout,re.M)]
    duration=max(times)/1e6 if times else 0
    bad=bool(re.search(r'HTTP error|Error during|partial file|Invalid data|Error while|Connection timed out',stderr,re.I))
    return {'pass':returncode==0 and bool(frames) and max(frames)>0 and duration>=seconds-.5 and not bad,
            'frames':max(frames) if frames else 0,'decoded_seconds':round(duration,3),'exit_code':returncode}
