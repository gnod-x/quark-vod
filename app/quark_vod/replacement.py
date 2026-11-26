"""Restart-safe source replacement: stage and verify before retiring the old object.

Callers hold the maintenance lease. A small durable journal keeps the candidate
row separate from the currently playable row and permits the same request to
resume an uncertain save without submitting it again.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import config, coordination, db, strm_manager
from .identity import Work
from .panshare_matrix import canonical_share


class ReplacementUnavailable(RuntimeError):
    def __init__(self, message, code="replacement_unavailable", status=409):
        super().__init__(message)
        self.code, self.status = code, status


def _journal_path(work):
    root = Path(config.DATA_DIR) / "replacement_intents"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / (hashlib.sha256(repr(work.key).encode()).hexdigest() + ".json")


def _write(path, state):
    fd, temporary = tempfile.mkstemp(prefix=".replacement-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _snapshot(work):
    if not config.VOD_JELLYFIN_ENABLED:
        return []
    snapshots = []
    for name in strm_manager.activation_paths(*work.args, **work.options):
        path = Path(name)
        if path.is_symlink() or not path.resolve().is_relative_to(Path(config.CINEMA2_DIR).resolve()):
            raise ReplacementUnavailable("片库入口边界无法确认，已保留旧源")
        snapshots.append({"path": str(path), "content": path.read_text(encoding="utf-8")})
    if not snapshots:
        raise ReplacementUnavailable('片库入口尚未就绪，已保留旧源')
    return snapshots


def _restore(state, remote):
    """Undo only this replacement's writes, never an unrelated concurrent edit."""
    for saved in state["strm"]:
        path = Path(saved["path"])
        if path.is_symlink() or not path.is_file():
            raise ReplacementUnavailable("旧入口恢复失败；已保留两端云文件", "rollback_failed")
        current = path.read_text(encoding="utf-8")
        if current == saved["content"]:
            continue
        pointed = strm_manager.parse_live_remote_path(current.strip())
        if not pointed or config.write_path(pointed) != config.write_path(remote or ""):
            raise ReplacementUnavailable("入口已变化，停止自动覆盖；已保留两端云文件", "rollback_failed")
        strm_manager._atomic_write(str(path), saved["content"].rstrip("\n"))


def _retire_old(state, transfer):
    old = db.get_transfer_by_id(state.get("old_id")) if state.get("old_id") else None
    if not old or old["status"] == "cleaned" or not old["remote_path"]:
        return True
    target = config.write_path(old["remote_path"] or "")
    root = config.write_path(config.QUARK_VOD_ROOT).rstrip("/")
    # Delete one exact old leaf; unknown siblings or shared references are retained.
    parts = target[len(root) + 1:].split("/") if target.startswith(root + "/") else []
    if len(parts) != 2 or not parts[0].startswith("vod_") or ".." in parts or not parts[1]:
        return False
    for other in db._conn().execute("SELECT * FROM transfers WHERE id<>? AND status<>'cleaned'", (old["id"],)):
        for value in (other["remote_path"], other["save_target"]):
            value = config.write_path(value or "").rstrip("/")
            if value and (value == target or value.startswith(target + "/") or target.startswith(value + "/")):
                return False
    # Recheck the published replacement on cleanup retries too. A manual
    # pending/rollback edit is a reason to preserve the old object.
    candidate = db.get_transfer_by_id(state["candidate_id"])
    if not candidate or candidate["status"] != "done" or not candidate["remote_path"]:
        return False
    if config.VOD_JELLYFIN_ENABLED and not state["strm"]:
        return False
    for saved in state["strm"]:
        current = strm_manager.parse_live_remote_path(strm_manager.read_strm(saved["path"]))
        if not current or config.write_path(current) != config.write_path(candidate["remote_path"]):
            return False
    try:
        if not transfer.delete_remote(target):
            return False
    except Exception:
        return False
    with db._conn() as con:
        con.execute("UPDATE transfers SET status='cleaned',remote_path=NULL,updated_at=? WHERE id=? AND remote_path=?",
                    (db.now(), old["id"], old["remote_path"]))
        con.execute("UPDATE cleanup_queue SET cleaned=1 WHERE transfer_id=?", (old["id"],))
    return True


def replace(req, transfer, finish, invalidate):
    work = Work(req.cat, req.title, req.season, req.episode, req.variant)
    if not canonical_share(req.share_url):
        raise ReplacementUnavailable("只接受夸克分享", status=422)
    with coordination.file_lock("work:" + repr(work.key), timeout=2):
        return _replace_locked(work, req, transfer, finish, invalidate)


def _replace_locked(work, req, transfer, finish, invalidate):
    path = _journal_path(work)
    state = json.loads(path.read_text()) if path.exists() else None
    if state:
        candidate = db.get_transfer_by_id(state["candidate_id"])
        if not candidate:
            raise ReplacementUnavailable("换源恢复记录不完整，已保留旧源")
        different = (canonical_share(candidate["share_url"]) != canonical_share(req.share_url)
                     or state["selected_fid"] != req.selected_fid)
        terminal_empty = (candidate["status"] in ("failed", "cleaned") and not any(
            candidate[key] for key in ("remote_path", "save_started_at", "save_task_id", "save_target", "save_file_fid")))
        if different and terminal_empty and state["phase"] == "preparing":
            path.unlink()
            return _replace_locked(work, req, transfer, finish, invalidate)
        if different and state["phase"] == "committed":
            if _retire_old(state, transfer):
                path.unlink()
            else:
                # Retain durable cleanup evidence without locking the work to
                # this already-published selection forever. The old transfer's
                # ordinary expiry queue also remains retryable.
                archive = path.parent / (path.stem + ".cleanup-" + str(state["candidate_id"]) + ".json")
                os.replace(path, archive)
            return _replace_locked(work, req, transfer, finish, invalidate)
        if different:
            raise ReplacementUnavailable("此集有尚未结束的换源任务，请先重试原选择或核查候选任务", "replacement_in_progress")
        old = db.get_transfer_by_id(state.get("old_id")) if state.get("old_id") else None
    else:
        old = db.find_done_transfer(*work.args, **work.options) or db.get_transfer(*work.args, **work.options)
        if old and canonical_share(old["share_url"]) == canonical_share(req.share_url) and not req.selected_fid:
            raise ReplacementUnavailable("新旧片源相同，请选择其他分享")
        if old and (old["save_started_at"] or old["save_task_id"] or old["save_target"]):
            raise ReplacementUnavailable("当前保存意图尚未确认，不能换源", "save_intent_unconfirmed")
        try:
            checked = transfer.inspect_share_video(req.share_url, req.title, req.season, req.episode,
                                                  selected_fid=req.selected_fid, **work.options)
        except Exception as exc:
            raise ReplacementUnavailable("新片源尚未通过身份与季集校验", status=422) from exc
        if not checked:
            raise ReplacementUnavailable("新片源尚未通过身份与季集校验", status=422)
        if old and req.selected_fid:
            # A different file in one share is a valid replacement; the same
            # deterministic slot would merely overwrite the current object.
            from .transfer import QuarkTransfer
            pwd_id, _ = QuarkTransfer.parse_share(req.share_url)
            selected = checked.get("share") or {}
            identity = repr((*work.args, work.variant, pwd_id, selected.get("fid"))).encode()
            slot = config.QUARK_VOD_ROOT + "/vod_" + hashlib.sha256(identity).hexdigest()[:24]
            if config.write_path(old["remote_path"] or "").startswith(config.write_path(slot) + "/"):
                raise ReplacementUnavailable("所选文件已是当前片源，请选择其他文件")
        snapshots = _snapshot(work)
        tid = db.create_transfer(*work.args, share_url=req.share_url, **work.options)
        state = {"candidate_id": tid, "old_id": old["id"] if old else None,
                 "phase": "preparing", "selected_fid": req.selected_fid,
                 "reason": req.reason, "strm": snapshots}
        _write(path, state)
        candidate = db.get_transfer_by_id(tid)
    tid, remote = state["candidate_id"], candidate["remote_path"]
    # A dedicated receipt is committed in the same SQLite transaction as the
    # audit and ban. Comparing share URLs is insufficient when selecting another
    # file from the same share, and activation itself can mark a row done.
    receipt = db._conn().execute("SELECT 1 FROM replacement_commits WHERE candidate_id=?", (tid,)).fetchone()
    if state["phase"] == "activating" and receipt:
        state["phase"] = "committed"
        _write(path, state)
    if state["phase"] != "committed":
        try:
            if state["phase"] == "activating":
                _restore(state, remote)
            if not remote or candidate["save_started_at"]:
                tid, remote = transfer.transfer(*work.args, req.share_url, selected_fid=req.selected_fid,
                                                transfer_id=tid, **work.options)
            # This row must not supersede the still-live source until activation.
            db.upsert_transfer(*work.args, _transfer_id=tid, status="found", **work.options)
            state["phase"] = "activating"
            _write(path, state)
            finish(work, tid, remote)
            if config.VOD_JELLYFIN_ENABLED:
                if not state["strm"]:
                    raise ReplacementUnavailable("新入口为空，已保留旧源")
                for saved in state["strm"]:
                    current = strm_manager.parse_live_remote_path(strm_manager.read_strm(saved["path"]))
                    if not current or config.write_path(current) != config.write_path(remote):
                        raise ReplacementUnavailable("新入口激活未确认，已保留旧源")
            # One SQLite commit publishes the source audit, ban and new active row.
            timestamp = db.now()
            with db._conn() as con:
                con.execute("UPDATE transfers SET status='done',error=NULL,updated_at=? WHERE id=?", (timestamp, tid))
                con.execute("INSERT OR REPLACE INTO replacement_commits(candidate_id,old_transfer_id,committed_at) VALUES(?,?,?)",
                            (tid, state.get("old_id"), timestamp))
                con.execute("INSERT OR REPLACE INTO source_audits(cat,title,season,episode,variant,has_quark_source,is_4k,best_share_url,best_share_title,checked_at,check_status) VALUES(?,?,?,?,?,1,0,?,?,?,'selected_file')",
                            (*work.args, work.variant, req.share_url, "", timestamp))
                if old and old["share_url"] and canonical_share(old["share_url"]) != canonical_share(req.share_url):
                    con.execute("INSERT OR REPLACE INTO dead_sources(source_url,source_type,reason,created_at,expires_at) VALUES(?,'panshare',?,?,?)",
                                (canonical_share(old["share_url"]) or old["share_url"], state["reason"], timestamp, timestamp + 7*86400))
            state["phase"] = "committed"
            _write(path, state)
        except Exception:
            if state["phase"] == "committed":
                raise
            db.upsert_transfer(*work.args, _transfer_id=tid, status="failed", **work.options)
            _restore(state, remote)
            state["phase"] = "preparing"
            _write(path, state)
            raise
    if state.get("old_id"):
        invalidate(state["old_id"])
    invalidate(tid)
    retired = _retire_old(state, transfer)
    if retired:
        path.unlink()
    return {"ok": True, "transfer_id": tid, "state": "ready", "cleanup_pending": not retired}
