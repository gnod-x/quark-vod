#!/usr/bin/env python3
"""Cloud-write test with temporary state; normal deletion may retain a recycle copy."""
import argparse
from contextlib import closing, redirect_stdout, redirect_stderr
import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
from unittest.mock import patch


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--transfer-id',type=int,required=True)
    ap.add_argument('--max-bytes',type=int,required=True)
    ap.add_argument('--output',required=True)
    ap.add_argument('--execute',action='store_true',help='required: creates and removes a cloud copy')
    args=ap.parse_args()
    if not args.execute:raise SystemExit('Explicit --execute required for cloud writes')
    base=Path('/home/dong/projects/hometheater')
    with closing(sqlite3.connect((base/'data/quark_plays.db').as_uri()+'?mode=ro',uri=True,timeout=2)) as con:
        row=con.execute("SELECT share_url,season,episode FROM transfers WHERE id=? AND status='done'",(args.transfer_id,)).fetchone()
    if not row:raise SystemExit('No selected done share')
    report={'time':datetime.datetime.now().astimezone().isoformat(),'source_transfer_id':args.transfer_id,'cloud_write_test':True,'production_db_written':False,
            'cleanup_scope':'normal_directory_only','recycle_cleanup_verified':False}
    test_name='vod_verify_'+uuid.uuid4().hex
    root='/夸克网盘/点播/'+test_name
    report['temporary_root']=root
    created=False
    with tempfile.TemporaryDirectory(prefix='cinema-transfer-') as tmp:
        os.environ.update(VOD_BASE_DIR=str(base),VOD_DATA_DIR=tmp+'/data',VOD_CINEMA2_DIR=tmp+'/cinema2',AI_PARSE_ENABLED='0')
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
        from quark_vod import config,db,strm_manager
        from quark_vod.openlist_client import OpenListClient
        from quark_vod.transfer import QuarkTransfer,QUARK_SHARE_DETAIL
        ol=OpenListClient()
        tr=QuarkTransfer(ol)
        try:
            token,pid,items=tr.resolve_share(row[0])
            total=0;count=0;queue=[items];visited=0
            while queue:
                entries=queue.pop(0)
                for item in entries:
                    visited+=1
                    if visited>100:raise RuntimeError('share inspection entry limit')
                    if item.get('dir'):
                        url=QUARK_SHARE_DETAIL+'&pwd_id='+urllib.parse.quote(pid)+'&stoken='+urllib.parse.quote(token)+'&pdir_fid='+urllib.parse.quote(item['fid'])+'&_page=1&_size=100'
                        data=tr._request(url,method='GET',timeout=10).get('data') or {}
                        children=data.get('list') or []
                        if int(data.get('_total') or 0)>len(children):raise RuntimeError('share inspection truncated')
                        queue.append(children)
                    else:
                        total+=item.get('size',0) or 0;count+=1
                        if total>args.max_bytes:raise RuntimeError('share too large')
            report.update(share_bytes=total,share_files=count)
            capacity=tr._request('https://drive.quark.cn/1/clouddrive/member?pr=ucpro&fr=pc',method='GET').get('data') or {}
            free=int(capacity.get('total_capacity') or 0)-int(capacity.get('use_capacity') or 0)
            if free<total+1024**3:raise RuntimeError('insufficient confirmed free capacity')
            if tr._get_quark_fid(root):raise RuntimeError('unique root unexpectedly exists')
            created=True
            ol.mkdir(root)
            config.QUARK_VOD_ROOT=root
            config.QUARK_PLAY_ROOT=config.play_path(root)
            season,episode=row[1],row[2]
            path=Path(config.CINEMA2_DIR)/'fixture'/test_name/'target.strm'
            path.parent.mkdir(parents=True)
            path.write_text(config.PENDING_BASE_URL+'/pending?'+urllib.parse.urlencode({'cat':'fixture','t':test_name,'s':season,'e':episode})+'\n')
            db.save_strm_map('fixture',test_name,season,episode,str(path))
            # Only library notifications are disabled: this fixture is outside
            # production cinema2. Cloud save/task/readback use the real code.
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()),patch.object(strm_manager,'trigger_jellyfin_refresh_item'):
                tid,remote=tr.transfer('fixture',test_name,season,episode,row[0])
                if not remote.startswith(root+'/'):raise RuntimeError('remote escaped fixture root')
                direct=ol.direct_url(remote)
                strm_manager.activate('fixture',test_name,season,episode,direct)
            report['state_done']=db.get_transfer_by_id(tid)['status']=='done'
            report['strm_live']=strm_manager.is_live(path.read_text().strip())
            req=urllib.request.Request(direct,headers={'Range':'bytes=0-65535','Accept-Encoding':'identity'})
            with urllib.request.urlopen(req,timeout=20) as response:
                data=response.read(65536)
                report['stream_http']=response.status
                report['stream_bytes']=len(data)
                report['media_signature']='mkv' if data.startswith(b'\x1aE\xdf\xa3') else 'mp4' if b'ftyp' in data[:16] else 'other'
            report['pipeline_passed']=report['state_done'] and report['strm_live'] and report['stream_http']==206 and len(data)==65536 and report['media_signature'] in ('mkv','mp4')
        except Exception as exc:
            report.update(pipeline_passed=False,error_type=type(exc).__name__)
        finally:
            if created:
                try:
                    ol.remove(root)
                    report['remote_removed']=tr._get_quark_fid(root) is None
                except Exception as exc:
                    report.update(remote_removed=False,cleanup_error_type=type(exc).__name__)
            else:report['remote_removed']=True
            if hasattr(db._local,'conn'):
                db._local.conn.close();del db._local.conn
    report['all_passed']=report.get('pipeline_passed',False) and report['remote_removed']
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if not report['all_passed']:raise SystemExit(1)


if __name__=='__main__':main()
