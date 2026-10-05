"""FastAPI 入口：POST /convert 速度图换算；/scores 谱例修订发布。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import math
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .db import get_store
from .tempo import TempoMap, TempoMapError, build_tempo_map, make_error

app = FastAPI(
    title="乐曲速度图换算 API",
    description=(
        "在乐谱位置（小节:拍:tick，拍按当前拍号分母对应的音符计）与秒数之间换算，"
        "支持阶梯（step）与线性（linear）BPM 速度点、拍号表及 PPQ；"
        "并支持带录音锚点的谱例创建、替换预览与修订发布（SQLite 持久化）。"
    ),
    version="1.1.0",
)


def _error_response(errors: list[dict], status_code: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": errors})


@app.exception_handler(TempoMapError)
async def tempo_map_error_handler(_request: Request, exc: TempoMapError) -> JSONResponse:
    # 语义校验错误（含出错数组元素索引 index）。
    return _error_response(exc.errors)


class RevisionConflict(Exception):
    """预期修订号与谱例当前修订不一致（乐观并发冲突）。"""

    def __init__(self, score_id: str, expected: int, current: int):
        self.score_id = score_id
        self.expected = expected
        self.current = current
        super().__init__(
            f"score {score_id}: expected revision {expected}, current {current}"
        )


@app.exception_handler(RevisionConflict)
async def revision_conflict_handler(
    _request: Request, exc: RevisionConflict
) -> JSONResponse:
    # 409：携带当前修订号，调用方需重新查询/预览后再试。
    return JSONResponse(
        status_code=409,
        content={
            "detail": [
                {
                    "loc": ["body", "expected_revision"],
                    "msg": (
                        f"预期修订号 {exc.expected} 与谱例当前修订 {exc.current} "
                        "不一致：期间已有其他发布，请重新查询并预览后再提交"
                    ),
                    "index": None,
                    "current_revision": exc.current,
                }
            ]
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Pydantic/请求体结构错误：保留 FastAPI 的 loc（天然含数组下标），
    # 同时展开为与语义错误一致的 {loc, msg, index} 结构。
    detail = []
    for err in exc.errors():
        loc = list(err.get("loc", []))
        index = next((x for x in reversed(loc) if isinstance(x, int)), None)
        detail.append(
            {
                "loc": [str(x) for x in loc],
                "msg": err.get("msg", "请求参数不合法"),
                "index": index,
            }
        )
    return _error_response(detail)


class TimeSignature(BaseModel):
    """只在小节首拍生效的拍号。第一份拍号前隐含 4/4。"""

    # 仅做类型约束；取值合法性（含数组下标）由引擎统一收集后返回 422。
    measure: int = Field(..., description="生效小节号（该小节首拍起）")
    numerator: int = Field(..., description="拍号分子，如 3/4 的 3；同时是每小节的拍数")
    denominator: int = Field(
        ...,
        description="拍号分母（2 的幂），如 3/4 的 4；一拍 = 4*PPQ/分母 tick，须为整数",
    )


class TempoPoint(BaseModel):
    """速度点。curve 决定该点到下一点区间内的速度走向。"""

    model_config = ConfigDict(json_schema_extra={})

    position: str = Field(
        ...,
        description=(
            "乐谱位置 '小节:拍:tick'，拍按当前拍号分母计"
            "（6/8 中一拍为八分音符），如 1:1:0、3:2:120"
        ),
    )
    bpm: float = Field(..., description="该点的 BPM，必须为正数")
    curve: Optional[Literal["step", "linear"]] = Field(
        "step",
        description=(
            "从该点到下一点：step=保持该 BPM（阶梯），linear=线性渐变到下一速度点。"
            "末点的曲线仅用于其后的保持段，实际按恒定 BPM 处理。"
        ),
    )


class ConvertRequest(BaseModel):
    ppq: int = Field(..., description="每个四分音符的 tick 数，如 480")
    time_signatures: list[TimeSignature] = Field(
        default_factory=list,
        description="拍号表，按小节首拍生效；空表或首项不在第 1 小节时前置隐含 4/4",
    )
    tempo_points: list[TempoPoint] = Field(
        ..., description="速度点表；首点必须在 1:1:0，位置严格递增"
    )
    position: Optional[str] = Field(
        None,
        description="待换算乐谱位置 '小节:拍:tick'（拍按当前拍号分母计）；与 seconds 二选一",
    )
    seconds: Optional[float] = Field(
        None, description="待换算时间（秒）；与 seconds 二选一（非负）"
    )


class ConvertResponse(BaseModel):
    ppq: int
    input_position: Optional[str]
    input_seconds: Optional[float]
    position: str = Field(..., description="规范乐谱位置 小节:拍:tick")
    measure: int
    beat: int
    tick: int
    total_ticks: int = Field(..., description="距乐谱起点的 tick 总数（PPQ 坐标）")
    seconds: float = Field(..., description="距乐谱起点的时间（秒）")
    roundtrip: "RoundTrip"


class RoundTrip(BaseModel):
    source: Literal["position", "seconds"]
    residual_ticks: float = Field(
        ...,
        description="位置 -> 秒 -> 位置（或反向）往返后，未量化位置与原位置的 tick 差绝对值",
    )
    residual_seconds: float = Field(..., description="以秒计的往返残差")
    position_roundtrip: str = Field(..., description="往返后再规范得到的乐谱位置")
    raw_total_ticks: float = Field(
        ..., description="秒 -> tick 反解出的未量化 tick（位置输入时等于 total_ticks）"
    )
    within_one_tick: bool = Field(..., description="往返误差是否不超过 1 tick")


ConvertResponse.model_rebuild()


# ---------------------------------------------------------------------- #
# 谱例（带录音锚点的修订发布）
# ---------------------------------------------------------------------- #
class AnchorIn(BaseModel):
    """录音锚点：乐谱位置 + 锁定标志。"""

    position: str = Field(
        ..., description="锚点乐谱位置 '小节:拍:tick'（拍按当前拍号分母计）"
    )
    locked: bool = Field(
        False,
        description=(
            "是否锁定。锁定锚点的秒数在发布时保持不变，且其相对创建时保存秒数的"
            "偏移不得超过录入时该位置一 tick 的时长，否则发布返回 422"
        ),
    )


class ScoreCreateRequest(BaseModel):
    ppq: int = Field(..., description="每个四分音符的 tick 数，如 480")
    time_signatures: list[TimeSignature] = Field(
        default_factory=list,
        description="拍号表，按小节首拍生效；空表或首项不在第 1 小节时前置隐含 4/4",
    )
    tempo_points: list[TempoPoint] = Field(
        ..., description="速度点表；首点必须在 1:1:0，位置严格递增"
    )
    anchors: list[AnchorIn] = Field(
        ..., description="录音锚点表（位置 + 锁定标志）；至少一个锁定"
    )


class AnchorState(BaseModel):
    index: int
    position: str
    locked: bool
    seconds: float = Field(..., description="当前保存的锚点秒数")


class ScoreResponse(BaseModel):
    score_id: str
    revision: int = Field(..., description="当前修订号；创建时为 0，每次成功发布 +1")
    ppq: int
    time_signatures: list[dict]
    tempo_points: list[dict]
    anchors: list[AnchorState]


class ReplacementRequest(BaseModel):
    """整份替换速度点表（预览/发布共用请求体）。"""

    expected_revision: int = Field(
        ..., ge=0, description="预期修订号；与谱例当前修订不一致时返回 409"
    )
    tempo_points: list[TempoPoint] = Field(
        ..., description="整份替换速度点表；首点必须在 1:1:0，位置严格递增"
    )


class AnchorPreview(BaseModel):
    index: int
    position: str
    locked: bool
    original_seconds: float = Field(..., description="当前保存的锚点秒数")
    new_seconds: float = Field(..., description="按替换速度图计算的锚点秒数")
    offset_seconds: float = Field(..., description="新秒数 - 原秒数")
    tolerance_seconds: float = Field(
        ..., description="录入时该位置一 tick 的时长（锁定锚点的漂移容忍度）"
    )
    lock_conflict: bool = Field(
        ..., description="锁定锚点偏移是否超过容忍度（为真时发布将返回 422）"
    )


class PreviewResponse(BaseModel):
    score_id: str
    revision: int = Field(..., description="本预览所基于的当前修订号")
    has_lock_conflicts: bool
    anchors: list[AnchorPreview]


@app.get("/")
async def root() -> dict:
    return {
        "service": "tempo-map-converter",
        "docs": "/docs",
        "convert": "/convert",
        "scores": "/scores",
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/convert", response_model=ConvertResponse)
async def convert(req: ConvertRequest) -> ConvertResponse:
    if req.seconds is not None and not (
        isinstance(req.seconds, (int, float))
        and not isinstance(req.seconds, bool)
        and math.isfinite(req.seconds)
        and req.seconds >= 0
    ):
        raise TempoMapError(
            [
                {
                    "loc": ["body", "seconds"],
                    "msg": "seconds 必须是有限的非负数",
                    "index": None,
                }
            ]
        )

    tmap = build_tempo_map(
        ppq=req.ppq,
        raw_meters=[m.model_dump() for m in req.time_signatures],
        raw_points=[
            {"position": p.position, "bpm": p.bpm, "curve": p.curve}
            for p in req.tempo_points
        ],
    )

    if (req.position is None) == (req.seconds is None):
        raise TempoMapError(
            [
                {
                    "loc": ["body"],
                    "msg": "position 与 seconds 必须且只能提供一个",
                    "index": None,
                }
            ]
        )

    if req.position is not None:
        total_ticks = tmap.mbt_to_ticks(req.position)
        seconds = tmap.seconds_at_tick(total_ticks)
        # 往返：位置 -> 秒 -> 位置
        raw_ticks = tmap.tick_at_seconds(seconds)
        back_seconds = tmap.seconds_at_tick(raw_ticks)
        input_position = req.position
        input_seconds = None
        source = "position"
    else:
        seconds = float(req.seconds)  # type: ignore[arg-type]
        raw_ticks = tmap.tick_at_seconds(seconds)
        total_ticks = int(math.floor(raw_ticks + 0.5))
        # 往返：秒 -> 位置（量化到 tick）-> 秒
        back_seconds = tmap.seconds_at_tick(total_ticks)
        input_position = None
        input_seconds = float(req.seconds)  # type: ignore[arg-type]
        source = "seconds"

    mbt = tmap.ticks_to_mbt(total_ticks)
    back_mbt = tmap.ticks_to_mbt(int(math.floor(raw_ticks + 0.5)))

    # 残差始终以“反解出的未量化 tick”与量化 tick 之差衡量（<= 1 tick 即合格）。
    residual_ticks = abs(raw_ticks - total_ticks)
    residual_seconds = abs(back_seconds - seconds)

    roundtrip = RoundTrip(
        source=source,
        residual_ticks=residual_ticks,
        residual_seconds=residual_seconds,
        position_roundtrip=str(back_mbt),
        raw_total_ticks=raw_ticks,
        within_one_tick=residual_ticks <= 1.0 + 1e-9,
    )

    return ConvertResponse(
        ppq=req.ppq,
        input_position=input_position,
        input_seconds=input_seconds,
        position=str(mbt),
        measure=mbt.measure,
        beat=mbt.beat,
        tick=mbt.tick,
        total_ticks=total_ticks,
        seconds=seconds,
        roundtrip=roundtrip,
    )


# ---------------------------------------------------------------------- #
# 谱例：创建 / 查询 / 替换预览 / 修订发布
# ---------------------------------------------------------------------- #
def _build_map(ppq: int, raw_meters: list[dict], points: list[TempoPoint]) -> TempoMap:
    """用既有引擎构建速度图（沿用拍号/速度点校验与线性段计时）。"""
    return build_tempo_map(
        ppq=ppq,
        raw_meters=raw_meters,
        raw_points=[p.model_dump() for p in points],
    )


def _score_response(score: dict) -> ScoreResponse:
    return ScoreResponse(
        score_id=score["score_id"],
        revision=score["revision"],
        ppq=score["ppq"],
        time_signatures=score["time_signatures"],
        tempo_points=score["tempo_points"],
        anchors=[
            AnchorState(
                index=a["index"],
                position=a["position"],
                locked=a["locked"],
                seconds=a["seconds"],
            )
            for a in score["anchors"]
        ],
    )


def _get_score_or_404(score_id: str) -> dict:
    score = get_store().get_score(score_id)
    if score is None:
        raise HTTPException(status_code=404, detail=f"谱例 {score_id!r} 不存在")
    return score


def _check_revision(score: dict, expected: int) -> None:
    current = score["revision"]
    if expected != current:
        raise RevisionConflict(score["score_id"], expected, current)


def _preview_anchor(anchor: dict, tmap: TempoMap) -> AnchorPreview:
    new_seconds = tmap.seconds_at_tick(anchor["tick"])
    offset = new_seconds - anchor["seconds"]
    conflict = anchor["locked"] and abs(offset) > anchor["tick_seconds"]
    return AnchorPreview(
        index=anchor["index"],
        position=anchor["position"],
        locked=anchor["locked"],
        original_seconds=anchor["seconds"],
        new_seconds=new_seconds,
        offset_seconds=offset,
        tolerance_seconds=anchor["tick_seconds"],
        lock_conflict=conflict,
    )


@app.post("/scores", response_model=ScoreResponse, status_code=201)
async def create_score(req: ScoreCreateRequest) -> ScoreResponse:
    """创建谱例：保存 PPQ/拍号/速度点与锚点，按原图计算并保存各锚点秒数。"""
    tmap = _build_map(
        req.ppq, [m.model_dump() for m in req.time_signatures], req.tempo_points
    )

    # 锚点校验：位置合法（带索引的 422），且至少一个锁定。
    errors: list[dict] = []
    anchors: list[dict] = []
    for i, a in enumerate(req.anchors):
        try:
            tick = tmap.mbt_to_ticks(a.position, loc=("body", "anchors", i, "position"))
        except TempoMapError as exc:
            for e in exc.errors:
                e["index"] = i
                errors.append(e)
        else:
            anchors.append(
                {
                    "index": i,
                    "position": a.position,
                    "tick": tick,
                    "locked": a.locked,
                    # 按原图计算秒数；并保存录入时该位置一 tick 的时长，
                    # 作为发布后锁定漂移的容忍度。
                    "seconds": tmap.seconds_at_tick(tick),
                    "tick_seconds": tmap.tick_seconds_at(tick),
                }
            )
    if not any(a.locked for a in req.anchors):
        errors.append(
            make_error("至少需要一个锁定锚点（locked=true）", "body", "anchors")
        )
    if errors:
        raise TempoMapError(errors)

    score = get_store().create_score(
        ppq=req.ppq,
        time_signatures=[m.model_dump() for m in req.time_signatures],
        tempo_points=[p.model_dump() for p in req.tempo_points],
        anchors=anchors,
    )
    return _score_response(score)


@app.get("/scores/{score_id}", response_model=ScoreResponse)
async def get_score(score_id: str) -> ScoreResponse:
    """查询谱例：返回同一修订版本的速度图与锚点。"""
    return _score_response(_get_score_or_404(score_id))


@app.post("/scores/{score_id}/preview", response_model=PreviewResponse)
async def preview_replacement(score_id: str, req: ReplacementRequest) -> PreviewResponse:
    """预览整份替换速度点表：返回各锚点原秒数/新秒数/偏移/锁定冲突，不改数据。"""
    score = _get_score_or_404(score_id)
    _check_revision(score, req.expected_revision)
    tmap = _build_map(score["ppq"], score["time_signatures"], req.tempo_points)
    anchors = [_preview_anchor(a, tmap) for a in score["anchors"]]
    return PreviewResponse(
        score_id=score_id,
        revision=score["revision"],
        has_lock_conflicts=any(a.lock_conflict for a in anchors),
        anchors=anchors,
    )


@app.post("/scores/{score_id}/publish", response_model=ScoreResponse)
async def publish_replacement(score_id: str, req: ReplacementRequest) -> ScoreResponse:
    """发布整份替换速度点表。

    按当前修订重新计算：锁定锚点相对创建时保存的秒数若偏移超过录入时
    该位置一 tick 的时长，返回带锚点索引的 422 且数据不变；成功则在一个
    事务内更新速度图、未锁定锚点秒数与修订号（锁定锚点秒数不变）。
    """
    store = get_store()
    with store.write_tx():
        score = _get_score_or_404(score_id)
        _check_revision(score, req.expected_revision)
        tmap = _build_map(score["ppq"], score["time_signatures"], req.tempo_points)

        errors: list[dict] = []
        new_seconds: dict[int, float] = {}
        for a in score["anchors"]:
            sec = tmap.seconds_at_tick(a["tick"])
            if a["locked"]:
                offset = sec - a["seconds"]
                if abs(offset) > a["tick_seconds"]:
                    errors.append(
                        make_error(
                            f"锁定锚点 {a['position']} 的秒数相对创建时保存值 "
                            f"{a['seconds']:.6f} 偏移 {offset:+.6f} 秒，超过录入时"
                            f"该位置一 tick 的时长 {a['tick_seconds']:.6f} 秒",
                            "body",
                            "anchors",
                            a["index"],
                            index=a["index"],
                        )
                    )
            else:
                new_seconds[a["index"]] = sec
        if errors:
            # 事务回滚，已存数据不变。
            raise TempoMapError(errors)

        store.apply_publish(
            score_id,
            tempo_points=[p.model_dump() for p in req.tempo_points],
            anchor_seconds=new_seconds,
        )
        updated = store.get_score(score_id)
        assert updated is not None
    return _score_response(updated)
