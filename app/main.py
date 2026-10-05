"""FastAPI 入口：POST /convert 速度图换算。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import math
from typing import Literal, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .tempo import TempoMapError, build_tempo_map

app = FastAPI(
    title="乐曲速度图换算 API",
    description=(
        "在乐谱位置（小节:拍:tick，拍按当前拍号分母对应的音符计）与秒数之间换算，"
        "支持阶梯（step）与线性（linear）BPM 速度点、拍号表及 PPQ。"
    ),
    version="1.0.0",
)


def _error_response(errors: list[dict], status_code: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": errors})


@app.exception_handler(TempoMapError)
async def tempo_map_error_handler(_request: Request, exc: TempoMapError) -> JSONResponse:
    # 语义校验错误（含出错数组元素索引 index）。
    return _error_response(exc.errors)


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


@app.get("/")
async def root() -> dict:
    return {"service": "tempo-map-converter", "docs": "/docs", "convert": "/convert"}


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
