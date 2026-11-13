#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
name_parser 离线回归测试（不依赖网络、不访问模型、不写生产库）
================================================================================
覆盖：
  1. 规则分级：强证据 / 季号冲突 / 弱匹配 / 未命中 四分区判定是否正确
  2. 规则分级与 transfer.extract_episode_num 的规则 A/B/C 口径必须一致
  3. 红线 8 黄金样本（7 部被误杀的电影 + 4 个剧集回归样本）的 tier 与规则结果
  4. AI 输出硬校验：值域 / 置信度 / 下标完整性
  5. 熔断与回落：模型异常时结果必须等于纯规则结果

运行：python3 -I tests/test_name_parser.py
"""
import os
import sys
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "app"))

FAILS = []


def check(desc, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}  got={got!r} want={want!r}")
    if not ok:
        FAILS.append(desc)


def main():
    # ── 测试隔离：把 STATE_DB 指向临时目录，避免污染生产库 ──
    import tempfile
    tmp = tempfile.mkdtemp(prefix="np_test_")
    os.environ["VOD_DATA_DIR"] = tmp
    os.environ["AI_PARSE_ENABLED"] = "0"     # 本测试一律不触网

    from quark_vod import config, db, name_parser
    db.init_db()

    print("\n=== 1. 规则分级（四分区）===")
    cases = [
        # (文件名, 期望 tier, 期望规则集数)
        ("S01E04.mkv",                       "strong",   4),
        # 现有规则对「集数后接空格再接文字」同样不识别（落到规则 B 季号冲突）。
        # name_parser 必须与主路径同口径，不得擅自放宽。
        ("S04E37 - 标题.mkv",                "conflict", None),
        ("第64集.mp4",                        "strong",   64),
        ("[04].mp4",                         "weak",     4),
        ("01.mp4",                           "weak",     1),
        # 规则 D 先于 A/B（红线 8 的已知缺口）：_60 被读成 60，由 AI 复核修正
        ("宗师叶问2.2010.4K_HDR_60.DDP5 1.mkv", "weak",   60),
        ("惊蛰无声.d3 1.mkv",                 "weak",     1),
        ("死侍与金刚狼.2024.H.264.1080.HDR10.mp4", "none", None),
        ("罗小黑战记.The.Legend.of.Hei.2019.BluRay.2160p.mkv", "none", None),
        ("后室.2025.WEB-DL.DDP5.1.H.265.mkv", "none",   None),
        ("痴迷.mp4",                          "none",     None),
        ("给阿嬷的情书.mkv",                   "none",     None),
        ("凡人修仙传.第37集.2160p.mkv",        "strong",   37),
        # 季号冲突：S02E05 对目标 season=1 必须判 conflict 且返回 None
        ("某剧.S02E05.2160p.mkv",            "conflict", None),
        # 2026-11-13：集数恰为年份形态，清洗层会误剥成 S01E10 → 必须 downgrade 为 weak
        ("某剧.S01E2010.mkv",                "weak",     None),
        # 电影名中的年份不得被误判为 weak（必须保持 none）
        ("影片.2020.1080p.mkv",              "none",     None),
    ]
    for name, want_tier, want_ep in cases:
        ep, tier = name_parser.classify_by_rules(name, season=1)
        check(f"classify {name}", (tier, ep), (want_tier, want_ep))

    print("\n=== 2. 与 transfer.extract_episode_num 口径一致性（强证据/冲突分区）===")
    from quark_vod.transfer import QuarkTransfer as Transfer
    for name, want_tier, _ in cases:
        rule_ep = Transfer.extract_episode_num(name, season=1)
        tier_ep, tier = name_parser.classify_by_rules(name, season=1)
        if tier in ("strong", "conflict"):
            check(f"口径一致 {name}", (tier_ep, rule_ep), (rule_ep, rule_ep))

    print("\n=== 3. AI 输出硬校验 ===")
    names = ["a.mkv", "b.mkv", "c.mkv"]
    orig = name_parser.urllib.request.urlopen
    def fake_factory(content):
        def fake(req, timeout=None):
            payload = {"choices": [{"message": {"content": content}}]}
            class R:
                def read(self): return __import__("json").dumps(payload).encode()
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()
        return fake

    name_parser.urllib.request.urlopen = fake_factory(
        '[{"i":0,"episode":3,"confidence":0.9},'
        ' {"i":1,"episode":0,"confidence":0.9},'      # 值域非法 → None
        ' {"i":2,"episode":5,"confidence":0.5}]')     # 置信度不足 → None
    out = name_parser._ask_model(names, 1)
    check("值域非法判 None", out[1][0], None)
    check("低置信度判 None", out[2][0], None)
    check("正常采信", out[0][0], 3)

    name_parser.urllib.request.urlopen = fake_factory(
        '[{"i":0,"episode":3,"confidence":0.9}]')     # 下标不全
    try:
        name_parser._ask_model(names, 1)
        check("下标不完整应抛异常", "no-raise", "raise")
    except ValueError:
        check("下标不完整应抛异常", "raise", "raise")

    name_parser.urllib.request.urlopen = fake_factory(
        '```json\n[{"i":0,"episode":7,"confidence":0.9},{"i":1,"episode":null,"confidence":0.9},{"i":2,"episode":null,"confidence":0.9}]\n```')
    check("Markdown 围栏可解析", name_parser._ask_model(names, 1)[0][0], 7)

    # 2026-11-13 实测缺陷：模型下标整体偏移/漏项时，必须按 file_name 归位而不是错挂。
    name_parser.urllib.request.urlopen = fake_factory(
        '[{"i":1,"file_name":"b.mkv","episode":9,"confidence":0.9},'
        ' {"i":2,"file_name":"c.mkv","episode":12,"confidence":0.9},'
        ' {"i":0,"file_name":"a.mkv","episode":null,"confidence":0.9}]')
    out = name_parser._ask_model(names, 1)
    check("乱序按下标之外的 file_name 归位", (out[0][0], out[1][0], out[2][0]), (None, 9, 12))

    name_parser.urllib.request.urlopen = fake_factory(
        '[{"i":1,"file_name":"b.mkv","episode":9,"confidence":0.9}]')  # 漏掉 a、c
    try:
        name_parser._ask_model(names, 1)
        check("条目缺失应抛异常", "no-raise", "raise")
    except ValueError:
        check("条目缺失应抛异常", "raise", "raise")

    name_parser.urllib.request.urlopen = orig

    print("\n=== 4. 模型异常时回落纯规则 ===")
    name_parser.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    os.environ["AI_PARSE_ENABLED"] = "1"
    name_parser.config.AI_PARSE_ENABLED = True
    name_parser._mem.clear()
    try:
        db._conn().execute("DELETE FROM name_parse_cache")
        db._conn().commit()
    except Exception:
        pass
    batch = ["S01E04.mkv", "[04].mp4", "惊蛰无声.d3 1.mkv", "痴迷.mp4"]
    got = name_parser.parse_names_batch(batch, 1)
    want = {n: Transfer.extract_episode_num(n, 1) for n in batch}
    check("模型不可用时结果等于纯规则", got, want)
    name_parser.urllib.request.urlopen = orig

    print("\n=== 4b. 4K 判定（2026-11-13 根治 HDR≠4K）===")
    from quark_vod.auditor import is_4k_candidate
    G = 1024 ** 3
    for fn, size, series, want in [
        ("某剧.S01E05.1080p.HDR.mkv",     0,     True,  False),  # 旧实现误判 True
        ("某剧.S01E05.720p.DV.mkv",       0,     True,  False),  # 旧实现误判 True
        ("某剧.S01E05.1080p.REMUX.mkv",   5 * G, True,  False),  # 体积大但显式 1080p
        ("某剧.S01E05.2160p.mkv",         0,     True,  True),
        ("某剧.S01E05.4K.mkv",            0,     True,  True),
        ("某剧.S01E05.UHD.HDR.mkv",       0,     True,  True),
        ("某剧.S01E05.mkv",               2 * G, True,  True),   # 未标注，体积推断
        ("某剧.S01E05.mkv",               0,     True,  False),
        ("Movie.2020.mkv",                5 * G, False, True),
    ]:
        check(f"4K {fn} {size // G}GB", is_4k_candidate(fn, size, is_series=series), want)

    print("\n=== 4c. title_judge 边界与调整策略 ===")
    from quark_vod import title_judge as tj
    from quark_vod.search import SearchResult, score_search_result

    def _mk(n):
        return SearchResult(engine="panshare", source="t", name=n, year="",
                            poster="", episodes=[], share_url="http://x/" + n)

    check("高分为不升分（85 同作保持 85）", tj._apply(85.0, 0.0), 85.0)
    check("低分为可升分（20 同作 → 60）", tj._apply(20.0, tj.MAX_UP), 60.0)
    check("不同作品可降分", tj._apply(85.0, -tj.MAX_DOWN), 45.0)
    check("分数不下穿 0", tj._apply(20.0, -tj.MAX_DOWN), 0.0)
    check("分数不上穿 100", tj._apply(90.0, tj.MAX_UP), 100.0)
    check("空结果零副作用", tj.judge([], "测试"), {})
    # 100 分强匹配绝不进模型（边界上限 90）——用打桩验证 AI 未被调用
    _called = []
    _orig_ask = tj._ask
    tj._ask = lambda *a, **k: _called.append(1) or (_ for _ in ()).throw(RuntimeError("不应调用"))
    try:
        ov = tj.judge([_mk("恶人 1080p")], "恶人", season=0, episode=0)
        check("100 分候选不进 AI", (_called, ov), ([], {}))
    finally:
        tj._ask = _orig_ask

    print("\n=== 5. 未命中（none）文件不得因 AI 缺席而丢失结果 ===")
    check("返回项完整", set(got.keys()), set(batch))

    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项未通过")
        for f in FAILS:
            print("  -", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
