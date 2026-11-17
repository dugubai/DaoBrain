"""Local GREEN canonical corpus: lexical simulation, not Qdrant/vector RAG."""
from fastapi import Depends, FastAPI

from common.catalog import search
from common.health import health_router
from common.schemas import RetrievalRequest, Retrieved, Zone
from common.security import require_internal

app = FastAPI(title="DaoBrain rag_green — lexical simulation")
app.include_router(health_router("rag_green"))


@app.post("/retrieve", response_model=list[Retrieved], dependencies=[Depends(require_internal)])
def retrieve(req: RetrievalRequest) -> list[Retrieved]:
    return search(req.query, req.top_k, Zone.GREEN)
