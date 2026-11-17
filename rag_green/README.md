# rag_green

⑦ 本地绿区RAG

`POST /retrieve`已实现内网鉴权与可信绿区小样本词法检索；每条证据带版本、hash和来源区，无命中返回空列表，不回退红区。Qdrant向量检索尚未接入。
