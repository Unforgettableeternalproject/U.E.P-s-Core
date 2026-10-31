"""A/B 二選一盲測網站：載入、作答、存檔、彙整與去識別化。fixture 全在 tmp_path。"""
import http.client
import importlib.util
import json
import struct
import threading
import wave
from pathlib import Path

import pytest

WEBAPP = Path(__file__).resolve().parents[1] / "webapp"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"blind_{name}", WEBAPP / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


server = _load("server")
tally = _load("tally")

SID = "a" * 32
SYSTEMS = {"c1": ("sysA", {"temp": 0.7}), "c2": ("sysB", {"temp": 0.5}), "c3": ("sysA", {"temp": 0.9})}


def _silence(path, ms=100):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * (16 * ms))


def _write_manifest(base, items):
    (base / "manifest.json").write_text(json.dumps(
        {"set": base.name, "created": "2026-01-01", "description": "假資料", "items": items},
        ensure_ascii=False), encoding="utf-8")


def _cands(base, n, cids):
    out = []
    for cid in cids:
        system, params = SYSTEMS[cid]
        fname = f"i{n}_{cid}.wav"
        _silence(base / "audio" / fname)
        out.append({"cand_id": cid, "system": system, "params": params,
                    "file": f"audio/{fname}", "metrics": {"sim": 0.9}})
    return out


@pytest.fixture()
def sets_dir(tmp_path):
    """item1：3 個候選（含兩個路徑穿越的壞候選）→ 3 題；item2：2 個候選 → 1 題。"""
    root = tmp_path / "blind_sets"
    base = root / "demo"
    (base / "audio").mkdir(parents=True)
    _silence(base / "secret.wav")  # 穿越用：manifest 指向 audio 目錄外的檔案
    item1 = {"item_id": "item1", "text": "文本1", "lang": "zh", "emotion": "happy",
             "candidates": _cands(base, 1, ["c1", "c2", "c3"]) + [
                 {"cand_id": "evil", "system": "x", "params": {}, "file": "../secret.wav", "metrics": {}},
                 {"cand_id": "evil2", "system": "x", "params": {}, "file": "audio/../secret.wav", "metrics": {}}]}
    item2 = {"item_id": "item2", "text": "文本2", "lang": "en", "emotion": "sad",
             "candidates": _cands(base, 2, ["c1", "c2"])}
    _write_manifest(base, [item1, item2])
    return root


@pytest.fixture()
def conn(sets_dir):
    srv = server.make_server(str(sets_dir), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    yield c
    c.close()
    srv.shutdown()
    srv.server_close()


def _get(c, path, headers=None):
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    return r.status, r.read(), r


def _post(c, payload, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    c.request("POST", "/api/review", body=body, headers={"Content-Type": "application/json"})
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def _quiz(c, sid=SID):
    st, body, _ = _get(c, f"/api/quiz?set=demo&sid={sid}")
    assert st == 200
    return json.loads(body), body.decode("utf-8")


def _choice(q, pick="a", **over):
    """依題目回傳 payload；pick 為 'a' / 'b'（畫面上的位置）。"""
    payload = {"set": "demo", "sid": SID, "nickname": "小明/../x", "pair": q["pair"],
               "chosen": q[pick]["cand_id"], "shown_order": [q["a"]["cand_id"], q["b"]["cand_id"]],
               "elapsed_ms": 1234}
    payload.update(over)
    return payload


def test_flow_load_answer_save_tally(conn, sets_dir):
    st, body, _ = _get(conn, "/api/sets")
    sets = json.loads(body)["sets"]
    assert st == 200 and sets[0]["set"] == "demo" and sets[0]["pairs"] == 4

    quiz, text = _quiz(conn)
    assert quiz["total"] == 4 and len(quiz["questions"]) == 4 and quiz["done"] == []
    # 展開結果：item1 三組兩兩配對 + item2 一題；穿越的壞候選被丟掉
    assert sorted(q["pair"] for q in quiz["questions"]) == [
        "item1:c1:c2", "item1:c1:c3", "item1:c2:c3", "item2:c1:c2"]
    # 去識別化：回應不含任何 system / params / metrics 內容
    for leak in ("sysA", "sysB", "temp", "metrics", "system", "params", "sim", "evil", "secret"):
        assert leak not in text
    for q in quiz["questions"]:
        assert set(q) == {"pair", "text", "lang", "emotion", "a", "b"}

    # 同一 sid 重取，題序與左右位置不變（續答一致）
    assert _quiz(conn)[0]["questions"] == quiz["questions"]

    # 全部選畫面上的 A；按下即存，每題一次 POST
    for n, q in enumerate(quiz["questions"], 1):
        st, res = _post(conn, _choice(q))
        assert st == 200 and res["answered"] == n

    files = list((sets_dir / "demo" / "reviews").glob("*.json"))
    assert len(files) == 1
    assert "/" not in files[0].name and ".." not in files[0].name  # 暱稱的路徑字元已被濾除
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert len(doc["choices"]) == 4
    one = next(iter(doc["choices"].values()))
    assert set(one) >= {"pair", "chosen", "shown_order", "elapsed_ms"} and one["elapsed_ms"] == 1234

    # 續答：done 列出已答的 pair
    quiz2, _ = _quiz(conn)
    assert len(quiz2["done"]) == 4

    res = tally.tally_set(str(sets_dir), "demo")
    assert res["reviewers"] == 1
    rows = {(r["system"], r["params"]): r for r in res["rows"]}
    # 勝負加總必為 4 場：每場一勝一負
    assert sum(r["wins"] for r in rows.values()) == 4
    assert sum(r["games"] for r in rows.values()) == 8
    # item1:c1:c3 都是 sysA（params 不同）仍算兩個不同的對手
    assert rows[("sysA", '{"temp": 0.7}')]["games"] == 3
    md = tally.render_md({"demo": res}, False)
    assert "勝率" in md and "兩兩對戰矩陣" in md and "sysB" in md
    assert "system,params,wins,games,win_rate" in tally.render_csv({"demo": res}, False)
    assert "row_wins" in tally.render_matrix_csv({"demo": res})


def test_win_rate_matrix_and_by_lang(sets_dir):
    """手寫作答檔：驗證勝率、矩陣與 --by-lang 的數字。"""
    rdir = sets_dir / "demo" / "reviews"
    rdir.mkdir()
    # 兩位受測者在 item2 (sysA c1 vs sysB c2)：甲選 c2，乙選 c1；甲另在 item1 選 c2 勝 c1
    def doc(sid, choices):
        return {"set": "demo", "sid": sid, "nickname": sid,
                "choices": {p: {"pair": p, "chosen": ch, "shown_order": p.split(":")[1:], "elapsed_ms": 1}
                            for p, ch in choices}}
    (rdir / "a.json").write_text(json.dumps(doc("a" * 32, [("item2:c1:c2", "c2"), ("item1:c1:c2", "c2")])))
    (rdir / "b.json").write_text(json.dumps(doc("b" * 32, [("item2:c1:c2", "c1")])))
    # 舊的多維評分格式不計入也不報錯
    (rdir / "old.json").write_text(json.dumps({"sid": "c" * 32, "responses": {"item1": {"best": "c1"}}}))

    res = tally.tally_set(str(sets_dir), "demo")
    assert res["reviewers"] == 2
    rows = {(r["system"], r["params"]): r for r in res["rows"]}
    b = rows[("sysB", '{"temp": 0.5}')]
    assert (b["wins"], b["games"], b["reviewers"]) == (2, 3, 2)
    a = rows[("sysA", '{"temp": 0.7}')]
    assert (a["wins"], a["games"]) == (1, 3)
    m = res["matrices"][""]
    labels = m["labels"]
    ib, ia = (next(i for i, l in enumerate(labels) if "sysB" in l),
              next(i for i, l in enumerate(labels) if "0.7" in l))
    assert m["cells"][(ib, ia)] == (2, 3) and m["cells"][(ia, ib)] == (1, 3)

    byl = tally.tally_set(str(sets_dir), "demo", by_lang=True)
    zh = [r for r in byl["rows"] if r["lang"] == "zh"]
    en = [r for r in byl["rows"] if r["lang"] == "en"]
    assert {r["system"]: r["games"] for r in zh} == {"sysA": 1, "sysB": 1}
    assert {r["system"]: r["games"] for r in en} == {"sysA": 2, "sysB": 2}
    assert set(byl["matrices"]) == {"zh", "en"}
    assert "語言" in tally.render_md({"demo": byl}, True)


def test_explicit_pairs_field(tmp_path):
    root = tmp_path / "blind_sets"
    base = root / "p"
    (base / "audio").mkdir(parents=True)
    item = {"item_id": "i1", "text": "t", "lang": "zh", "emotion": "n",
            "candidates": _cands(base, 1, ["c1", "c2", "c3"]),
            "pairs": [["c3", "c1"], ["c1", "c3"], ["c2", "c9"], ["c2", "c2"]]}
    _write_manifest(base, [item])
    store = server.BlindStore(str(root))
    # 只保留合法、去重後的指定配對
    assert [p["pair"] for p in store.pairs("p")] == ["i1:c3:c1"]


def test_audio_serving_and_traversal(conn):
    st, body, r = _get(conn, "/audio/demo/item1/c1")
    assert st == 200 and body[:4] == b"RIFF" and r.getheader("Accept-Ranges") == "bytes"
    st, body, r = _get(conn, "/audio/demo/item1/c1", {"Range": "bytes=0-9"})
    assert st == 206 and len(body) == 10
    assert _get(conn, "/audio/demo/item1/evil")[0] == 404
    assert _get(conn, "/audio/demo/item1/evil2")[0] == 404
    assert _get(conn, "/audio/demo/item1/..%2f..%2fmanifest.json")[0] == 400
    assert _get(conn, "/audio/..%2f/x/y")[0] in (400, 404)
    assert _get(conn, "/audio/demo/manifest.json")[0] == 400
    assert _get(conn, "/api/quiz?set=..%2fdemo&sid=" + SID)[0] == 404
    assert _get(conn, "/api/quiz?set=demo&sid=bad")[0] == 400
    assert _get(conn, "/api/set/demo")[0] == 404  # 舊的含候選全貌端點已移除


def test_pages_have_no_dimension_inputs():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "<textarea" not in html and 'type="range"' not in html
    assert html.count("<input") == 1  # 只剩暱稱


def test_write_validation(conn, sets_dir):
    q = _quiz(conn)[0]["questions"][0]
    ok = _choice(q)
    other = "c9"
    assert _post(conn, dict(ok, chosen=other))[0] == 400
    assert _post(conn, dict(ok, chosen="evil"))[0] == 400
    assert _post(conn, dict(ok, pair="item9:c1:c2"))[0] == 400
    assert _post(conn, dict(ok, pair="item1:c1:evil"))[0] == 400
    assert _post(conn, dict(ok, shown_order=["c1"]))[0] == 400
    assert _post(conn, dict(ok, shown_order=["c1", "c9"]))[0] == 400
    assert _post(conn, dict(ok, elapsed_ms=-1))[0] == 400
    assert _post(conn, dict(ok, elapsed_ms="1"))[0] == 400
    assert _post(conn, dict(ok, set="../demo"))[0] == 404
    assert _post(conn, dict(ok, sid="../../x"))[0] == 400
    assert _post(conn, None, raw=b"x" * (server.MAX_BODY + 1))[0] == 413
    assert _post(conn, None, raw=b"not json")[0] == 400
    assert not (sets_dir / "demo" / "reviews").exists()
    # 過大的 elapsed_ms 會被截斷而非拒絕
    assert _post(conn, dict(ok, elapsed_ms=10 ** 12))[0] == 200
    f = next((sets_dir / "demo" / "reviews").glob("*.json"))
    saved = json.loads(f.read_text(encoding="utf-8"))["choices"][q["pair"]]
    assert saved["elapsed_ms"] == server.MAX_ELAPSED_MS
    # 同一題重答會覆蓋，不會多一筆
    assert _post(conn, _choice(q, "b"))[1]["answered"] == 1


def test_rate_limit(conn):
    q = _quiz(conn)[0]["questions"][0]
    codes = [_post(conn, _choice(q))[0] for _ in range(server.RATE_LIMIT[0] + 3)]
    assert 429 in codes and codes[0] == 200
