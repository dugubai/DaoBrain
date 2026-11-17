"""Typed-text segmentation simulation, NOT image OCR or PaddleOCR integration."""
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.health import health_router
from common.security import require_internal

app = FastAPI(title="DaoBrain OCR · typed-text simulation only")
app.include_router(health_router("ocr"))


class RecognizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["text"]
    text: str = Field(min_length=1, max_length=10000)
    page: int = Field(default=1, ge=1, le=10000, strict=True)

    @model_validator(mode="before")
    @classmethod
    def no_image_ocr(cls, value):
        if isinstance(value, dict) and (
            value.get("mode") in ("image", "base64", "image_base64")
            or any(key in value for key in ("image", "images", "image_base64", "base64", "image_url"))
        ):
            raise HTTPException(501, "Image/base64 OCR is not implemented. This endpoint only simulates segmentation of explicitly typed text (mode=text); no image decoding occurs.")
        return value

    @field_validator("text")
    @classmethod
    def bounded_lines(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Typed text must not be blank")
        if len(value.splitlines()) > 200:
            raise ValueError("At most 200 typed-text lines per simulated page")
        return value


@app.post("/recognize", dependencies=[Depends(require_internal)])
def recognize(req: RecognizeRequest):
    segments = [{"index": index, "page": req.page, "text": line,
                 "synthetic": True, "needs_review": True}
                for index, line in enumerate(req.text.splitlines(), start=1) if line.strip()]
    return {"status": "simulation", "simulated": True, "page": req.page,
            "segments": segments, "needs_review": True,
            "note": "Typed-text segmentation only; no image OCR, source verification or clinical use."}
