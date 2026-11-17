"""Local RED lexical simulation with invented fixtures and no real user secrets."""
from fastapi import Depends, FastAPI

from common.catalog import search
from common.health import health_router
from common.schemas import RetrievalRequest, Retrieved, Zone
from common.security import require_internal

app = FastAPI(title="DaoBrain rag_red — synthetic lexical simulation")
app.include_router(health_router("rag_red"))


@app.post("/retrieve", response_model=list[Retrieved], dependencies=[Depends(require_internal)])
def retrieve(req: RetrievalRequest) -> list[Retrieved]:
    return search(req.query, req.top_k, Zone.RED)
