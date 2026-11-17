"""Trusted GREEN sample lookup simulation; no uploads or Qdrant integration."""
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import ConfigDict

from common.catalog import catalog, search, trusted
from common.health import health_router
from common.schemas import RetrievalRequest, Retrieved, Zone
from common.security import require_internal

app = FastAPI(title="DaoBrain corpus_kb · sample simulation")
app.include_router(health_router("corpus_kb"))


class SearchRequest(RetrievalRequest):
    model_config = ConfigDict(extra="forbid")


@app.post("/search", response_model=list[Retrieved], dependencies=[Depends(require_internal)])
def search_samples(req: SearchRequest):
    """Lexical retrieval over the fixed trusted public sample catalog only."""
    return [item for item in search(req.query, req.top_k, Zone.GREEN)
            if item.zone == Zone.GREEN and trusted(item)][:req.top_k]


@app.get("/sources/{source_id}", response_model=Retrieved,
         dependencies=[Depends(require_internal)])
def source_lookup(
    source_id: str,
    version: str | None = Query(default=None, min_length=1, max_length=64),
    content_hash: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$"),
):
    """Return exact catalog provenance; mismatched versions/hashes are not found."""
    for item in catalog(Zone.GREEN):
        provenance = item.provenance
        if (item.zone == Zone.GREEN and trusted(item)
                and provenance.source_id == source_id
                and (version is None or provenance.version == version)
                and (content_hash is None or provenance.content_hash == content_hash)):
            return item.model_copy(deep=True)
    raise HTTPException(404, "Trusted GREEN sample source/version/hash not found")
