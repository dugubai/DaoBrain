# corpus_kb

组件·知识库与典籍检索溯源

`POST /search`提供内网鉴权的可信公开词法检索；`GET /sources/{source_id}`返回精确来源、版本与hash，未知来源404。无任意语料上传入口，尚未接入数据库或Qdrant向量检索。
