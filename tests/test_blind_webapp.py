"""通用多候選盲測網站：載入、作答、存檔、彙整與去識別化。fixture 全在 tmp_path。"""
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

SYSTEMS = {"c1": ("sysA", {"temp": 0.7}), "c2": ("sysB", {"temp": 0.5}), "c3": ("sysA", {"temp": 0.9})}


def _silence(path, ms=100):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * (16 * ms))


@pytest.fixture()
def sets_dir(tmp_path):
    root = tmp_path / "blind_sets"
    base = root / "demo"
    (base / "audio").mkdir(parents=True)
    items = []
    for n, (lang, emo) in enumerate([("zh", "happy"), ("en", "sad")], 1):
        cands = []
        for cid, (system, params) in SYSTEMS.items():
            fname = f"i{n}_{cid}.wav"
            _silence(base / "audio" / fname)
            cands.append({"cand_id": cid, "system": system, "params": params,
                          "file": f"audio/{fname}", "metrics": {"sim": 0.9}})
        items.append({"item_id": f"item{n}", "text": f"文本{n}", "lang": lang,
                      "emotion": emo, "candidates": cands})
    (base / "manifest.json").write_text(json.dumps(
        {"set": "demo", "created": "2026-01-01", "description": "假資料", "items": items},
        ensure_ascii=False), encoding="utf-8")
    # 穿越用：manifest 指向 audio 目錄外的檔案
    _silence(base / "secret.wav")
    items[0]["candidates"].append({"cand_id": "evil", "system": "x", "params": {},
                                   "file": "../secret.wav", "metrics": {}})
    items[0]["candidates"].append({"cand_id": "evil2", "system": "x", "params": {},
                                   "file": "audio/../secret.wav", "metrics": {}})
    (base / "manifest.json").write_text(json.dumps(
        {"set": "demo", "created": "2026-01-01", "description": "假資料", "items": items},
        ensure_ascii=False), encoding="utf-8")
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


def _answer(item_id, best="c2", score=4):
    return {"set": "demo", "sid": "a" * 32, "nickname": "小明/../x", "item_id": item_id,
            "order": ["c3", "c1", "c2"], "best": best, "comment": "不錯",
            "ratings": {c: {"timbre": score, "naturalness": score, "emotion": score,
                            "accent": 5 if c == "c2" else 2} for c in SYSTEMS}}


def test_flow_load_answer_save_tally(conn, sets_dir):
    st, body, _ = _get(conn, "/api/sets")
    assert st == 200 and json.loads(body)["sets"][0]["set"] == "demo"

    st, body, _ = _get(conn, "/api/set/demo")
    pub = json.loads(body)
    assert len(pub["items"]) == 2
    # 路徑穿越的候選必須被丟掉
    assert [c["cand_id"] for c in pub["items"][0]["candidates"]] == ["c1", "c2", "c3"]
    text = body.decode("utf-8")
    for leak in ("sysA", "sysB", "temp", "metrics", "system", "params", "sim"):
        assert leak not in text

    for item in pub["items"]:
        st, res = _post(conn, _answer(item["item_id"]))
        assert st == 200
    assert res["answered"] == 2

    files = list((sets_dir / "demo" / "reviews").glob("*.json"))
    assert len(files) == 1
    assert files[0].name.endswith("_小明_.._x.json") is False  # 路徑字元已被濾除
    assert "/" not in files[0].name and ".." not in files[0].name

    # 接續進度
    st, body, _ = _get(conn, f"/api/progress?set=demo&sid={'a' * 32}")
    prog = json.loads(body)
    assert set(prog["responses"]) == {"item1", "item2"}

    rows, reviewers = tally.tally_set(str(sets_dir), "demo")
    assert reviewers == 1
    by = {(r["system"], r["params"]): r for r in rows}
    b = by[("sysB", '{"temp": 0.5}')]
    assert b["best"] == 2 and b["ratings"] == 2 and b["accent"] == 5
    assert by[("sysA", '{"temp": 0.7}')]["best"] == 0
    md = tally.render_md({"demo": (rows, reviewers)}, False)
    assert "sysB" in md and "100%" in md
    assert "system,params" in tally.render_csv({"demo": (rows, reviewers)}, False)


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
    assert _get(conn, "/api/set/..%2fdemo")[0] == 404


def test_write_validation(conn, sets_dir):
    ok = _answer("item1")
    bad_score = json.loads(json.dumps(ok))
    bad_score["ratings"]["c1"]["timbre"] = 6
    assert _post(conn, bad_score)[0] == 400
    missing = json.loads(json.dumps(ok))
    del missing["ratings"]["c3"]
    assert _post(conn, missing)[0] == 400
    assert _post(conn, dict(ok, best="evil"))[0] == 400
    assert _post(conn, dict(ok, item_id="nope"))[0] == 400
    assert _post(conn, dict(ok, set="../demo"))[0] == 404
    assert _post(conn, dict(ok, sid="../../x"))[0] == 400
    assert _post(conn, dict(ok, comment="x" * 5000))[0] == 400
    assert _post(conn, None, raw=b"x" * (server.MAX_BODY + 1))[0] == 413
    assert _post(conn, None, raw=b"not json")[0] == 400
    assert not (sets_dir / "demo" / "reviews").exists()
