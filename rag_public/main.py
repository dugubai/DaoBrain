"""Public canonical corpus: authenticated lexical simulation, not vector RAG."""
from fastapi import Depends, FastAPI

from common.catalog import search
from common.health import health_router
from common.schemas import RetrievalRequest, Retrieved, Zone
from common.security import require_internal

app = FastAPI(title="DaoBrain rag_public — lexical simulation")
app.include_router(health_router("rag_public"))


@app.post("/retrieve", response_model=list[Retrieved], dependencies=[Depends(require_internal)])
def retrieve(req: RetrievalRequest) -> list[Retrieved]:
    return search(req.query, req.top_k, Zone.GREEN)
