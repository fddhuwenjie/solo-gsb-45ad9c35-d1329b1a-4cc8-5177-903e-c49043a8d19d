"""谱例（带录音锚点的修订发布）端到端测试。

数值基准（PPQ=480，全程 4/4）：
- 速度图：120 step @1:1:0，60 step @3:1:0；
- 锚点 A（锁定）2:1:0 -> tick 1920 -> 4 个四分音符 * 0.5s = 2.0s，
  一 tick 时长 = 60/(120*480) = 1/960 s；
- 锚点 B（未锁定）4:1:0 -> tick 5760 -> 两小节 120（4.0s）+ 一小节 60（4.0s）
  = 8.0s，一 tick 时长 = 60/(60*480) = 1/480 s。
"""

import math

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BASE_POINTS = [
    {"position": "1:1:0", "bpm": 120, "curve": "step"},
    {"position": "3:1:0", "bpm": 60, "curve": "step"},
]

ANCHORS = [
    {"position": "2:1:0", "locked": True},
    {"position": "4:1:0", "locked": False},
]

TICK_120 = 60.0 / (120 * 480)  # 锚点 A 录入时一 tick 的时长


def create_score(points=None, anchors=None, ppq=480, time_signatures=None):
    body = {
        "ppq": ppq,
        "time_signatures": time_signatures if time_signatures is not None else [],
        "tempo_points": points if points is not None else BASE_POINTS,
        "anchors": anchors if anchors is not None else ANCHORS,
    }
    return client.post("/scores", json=body)


@pytest.fixture()
def score_id():
    resp = create_score()
    assert resp.status_code == 201, resp.text
    return resp.json()["score_id"]


# ---------------------------------------------------------------------- #
# 创建
# ---------------------------------------------------------------------- #
def test_create_returns_id_revision0_and_anchor_seconds():
    resp = create_score()
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["score_id"]
    assert data["revision"] == 0
    assert data["ppq"] == 480
    assert data["tempo_points"] == BASE_POINTS
    anchors = data["anchors"]
    assert [a["position"] for a in anchors] == ["2:1:0", "4:1:0"]
    assert [a["locked"] for a in anchors] == [True, False]
    assert anchors[0]["seconds"] == pytest.approx(2.0)
    assert anchors[1]["seconds"] == pytest.approx(8.0)


def test_create_requires_at_least_one_locked_anchor():
    resp = create_score(anchors=[
        {"position": "2:1:0", "locked": False},
        {"position": "4:1:0", "locked": False},
    ])
    assert resp.status_code == 422
    assert any("锁定" in e["msg"] for e in resp.json()["detail"])


def test_create_anchor_position_validated_with_index():
    resp = create_score(anchors=[
        {"position": "2:1:0", "locked": True},
        {"position": "2:5:0", "locked": False},  # 4/4 下没有第 5 拍
    ])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(e.get("index") == 1 for e in detail)


def test_create_reuses_tempo_map_validation():
    # 首点不在 1:1:0 -> 422（沿用既有速度点校验）
    resp = create_score(points=[{"position": "1:2:0", "bpm": 120, "curve": "step"}])
    assert resp.status_code == 422
    assert any("1:1:0" in e["msg"] for e in resp.json()["detail"])


# ---------------------------------------------------------------------- #
# 查询
# ---------------------------------------------------------------------- #
def test_get_score_returns_same_version_map_and_anchors(score_id):
    resp = client.get(f"/scores/{score_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revision"] == 0
    assert data["tempo_points"] == BASE_POINTS
    assert [a["seconds"] for a in data["anchors"]] == [2.0, 8.0]


def test_get_unknown_score_404():
    assert client.get("/scores/nope").status_code == 404


# ---------------------------------------------------------------------- #
# 预览
# ---------------------------------------------------------------------- #
def test_preview_returns_offsets_and_conflicts_without_modifying(score_id):
    new_points = [
        {"position": "1:1:0", "bpm": 120, "curve": "step"},
        {"position": "3:1:0", "bpm": 240, "curve": "step"},
    ]
    resp = client.post(
        f"/scores/{score_id}/preview",
        json={"expected_revision": 0, "tempo_points": new_points},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revision"] == 0
    assert data["has_lock_conflicts"] is False
    a, b = data["anchors"]
    # 锁定锚点在第 3 小节变速点之前，不受影响
    assert a["original_seconds"] == pytest.approx(2.0)
    assert a["new_seconds"] == pytest.approx(2.0)
    assert a["offset_seconds"] == pytest.approx(0.0)
    assert a["lock_conflict"] is False
    assert a["tolerance_seconds"] == pytest.approx(TICK_120)
    # 未锁定锚点：4.0 + 4 个四分音符 * 0.25s = 5.0
    assert b["original_seconds"] == pytest.approx(8.0)
    assert b["new_seconds"] == pytest.approx(5.0)
    assert b["offset_seconds"] == pytest.approx(-3.0)
    assert b["lock_conflict"] is False

    # 预览不修改已存数据
    after = client.get(f"/scores/{score_id}").json()
    assert after["revision"] == 0
    assert after["tempo_points"] == BASE_POINTS
    assert [x["seconds"] for x in after["anchors"]] == [2.0, 8.0]


def test_preview_flags_lock_conflict(score_id):
    # 全图提速到 240：锁定锚点 A 新秒数 1.0，偏移 -1.0 远超一 tick 时长
    resp = client.post(
        f"/scores/{score_id}/preview",
        json={
            "expected_revision": 0,
            "tempo_points": [{"position": "1:1:0", "bpm": 240, "curve": "step"}],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["has_lock_conflicts"] is True
    a = data["anchors"][0]
    assert a["new_seconds"] == pytest.approx(1.0)
    assert a["lock_conflict"] is True


def test_preview_validates_replacement_points(score_id):
    resp = client.post(
        f"/scores/{score_id}/preview",
        json={
            "expected_revision": 0,
            "tempo_points": [{"position": "1:2:0", "bpm": 120, "curve": "step"}],
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------- #
# 发布
# ---------------------------------------------------------------------- #
def test_publish_success_updates_map_unlocked_seconds_and_revision(score_id):
    new_points = [
        {"position": "1:1:0", "bpm": 120, "curve": "step"},
        {"position": "3:1:0", "bpm": 240, "curve": "step"},
    ]
    resp = client.post(
        f"/scores/{score_id}/publish",
        json={"expected_revision": 0, "tempo_points": new_points},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revision"] == 1
    assert data["tempo_points"] == new_points
    # 锁定锚点秒数不变，未锁定锚点按新图重算
    assert [a["seconds"] for a in data["anchors"]] == [2.0, 5.0]

    # 查询返回同一版本的速度图与锚点
    got = client.get(f"/scores/{score_id}").json()
    assert got == data


def test_publish_locked_drift_beyond_one_tick_returns_422_and_keeps_data(score_id):
    resp = client.post(
        f"/scores/{score_id}/publish",
        json={
            "expected_revision": 0,
            "tempo_points": [{"position": "1:1:0", "bpm": 240, "curve": "step"}],
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert any(e.get("index") == 0 for e in detail)  # 带锁定锚点索引
    assert any("锁定锚点" in e["msg"] for e in detail)

    # 数据不变
    after = client.get(f"/scores/{score_id}").json()
    assert after["revision"] == 0
    assert after["tempo_points"] == BASE_POINTS
    assert [a["seconds"] for a in after["anchors"]] == [2.0, 8.0]


def test_publish_locked_drift_within_one_tick_is_allowed(score_id):
    # 120 -> 120.05：A 新秒数 4*60/120.05 ≈ 1.99917，偏移 ≈ -0.00083 < 1/960
    resp = client.post(
        f"/scores/{score_id}/publish",
        json={
            "expected_revision": 0,
            "tempo_points": [
                {"position": "1:1:0", "bpm": 120.05, "curve": "step"},
                {"position": "3:1:0", "bpm": 60, "curve": "step"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revision"] == 1
    # 锁定锚点秒数保持创建时的保存值
    assert data["anchors"][0]["seconds"] == pytest.approx(2.0)


def test_publish_stale_expected_revision_returns_409(score_id):
    new_points = [
        {"position": "1:1:0", "bpm": 120, "curve": "step"},
        {"position": "3:1:0", "bpm": 240, "curve": "step"},
    ]
    # 预览（基于修订 0）
    prev = client.post(
        f"/scores/{score_id}/preview",
        json={"expected_revision": 0, "tempo_points": new_points},
    )
    assert prev.status_code == 200, prev.text
    # 期间另有发布成功（修订 0 -> 1）
    first = client.post(
        f"/scores/{score_id}/publish",
        json={"expected_revision": 0, "tempo_points": new_points},
    )
    assert first.status_code == 200, first.text
    # 旧预期修订号再发布 -> 409，不能覆盖新版本
    stale = client.post(
        f"/scores/{score_id}/publish",
        json={
            "expected_revision": 0,
            "tempo_points": [{"position": "1:1:0", "bpm": 90, "curve": "step"}],
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"][0]["current_revision"] == 1
    # 旧预期修订号再预览 -> 同样 409
    stale_prev = client.post(
        f"/scores/{score_id}/preview",
        json={"expected_revision": 0, "tempo_points": new_points},
    )
    assert stale_prev.status_code == 409
    # 新版本未被覆盖
    after = client.get(f"/scores/{score_id}").json()
    assert after["revision"] == 1
    assert after["tempo_points"] == new_points


def test_publish_unknown_score_404():
    resp = client.post(
        "/scores/nope/publish",
        json={"expected_revision": 0, "tempo_points": BASE_POINTS},
    )
    assert resp.status_code == 404
    resp = client.post(
        "/scores/nope/preview",
        json={"expected_revision": 0, "tempo_points": BASE_POINTS},
    )
    assert resp.status_code == 404


def test_publish_linear_curve_uses_integral_timing():
    # 线性段计时沿用引擎：60 -> 120 线性跨一小节，锚点取段末
    resp = create_score(
        points=[
            {"position": "1:1:0", "bpm": 60, "curve": "linear"},
            {"position": "2:1:0", "bpm": 120, "curve": "step"},
        ],
        anchors=[
            {"position": "1:1:0", "locked": True},
            {"position": "2:1:0", "locked": False},
        ],
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["score_id"]
    # 60->120 线性跨 1920 tick：ΔT = 60*1920/(60*480) * ln2 = 4 ln2
    assert resp.json()["anchors"][1]["seconds"] == pytest.approx(4 * math.log(2))

    # 发布新图：整段恒定 120，未锁定锚点重算为 2.0s
    pub = client.post(
        f"/scores/{sid}/publish",
        json={
            "expected_revision": 0,
            "tempo_points": [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
        },
    )
    assert pub.status_code == 200, pub.text
    assert pub.json()["anchors"][1]["seconds"] == pytest.approx(2.0)
    # 锁定锚点（1:1:0，秒数 0）不变
    assert pub.json()["anchors"][0]["seconds"] == pytest.approx(0.0)


# ---------------------------------------------------------------------- #
# SQLite 持久化（模拟重启后重开同一数据库文件）
# ---------------------------------------------------------------------- #
def test_store_persists_across_reopen(tmp_path):
    from app.db import ScoreStore

    path = str(tmp_path / "scores.db")
    store = ScoreStore(path)
    created = store.create_score(
        ppq=480,
        time_signatures=[],
        tempo_points=BASE_POINTS,
        anchors=[
            {
                "index": 0,
                "position": "2:1:0",
                "tick": 1920,
                "locked": True,
                "seconds": 2.0,
                "tick_seconds": TICK_120,
            }
        ],
    )
    # 模拟重启：在同一文件上重新打开
    reopened = ScoreStore(path)
    loaded = reopened.get_score(created["score_id"])
    assert loaded is not None
    assert loaded["revision"] == 0
    assert loaded["tempo_points"] == BASE_POINTS
    assert loaded["anchors"][0]["seconds"] == pytest.approx(2.0)
    assert loaded["anchors"][0]["locked"] is True
