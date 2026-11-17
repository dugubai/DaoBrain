"""Trusted component catalog. Capabilities never assert observed runtime success."""
import hashlib
import json


def _item(key, title, group, runtime, directory, port, limitation, editable=True):
    return {"id": key, "title": title, "group": group, "runtime": runtime,
            "directory": directory, "health_port": port, "limitation": limitation,
            "editable": editable, "config_schema": {"enabled": "bool", "request_timeout_s": "int:1:120"}}


_COMPONENTS = [
    _item("gateway", "入口网关", "legacy", "gateway", "gateway", 8000, "旧受控模拟入口"),
    _item("agent_orchestrator", "任务编排", "legacy", "agent_orchestrator", "agent_orchestrator", 8002, "有界模拟编排"),
    _item("rag_public", "公开检索", "legacy", "rag_public", "rag_public", 8003, "固定公开语料词法检索，非向量RAG"),
    _item("hybrid_iface", "混合分派", "legacy", "hybrid_iface", "hybrid_iface", 8004, "受控分派，断链不回退"),
    _item("external_model", "外部模型接口", "legacy", "external_model", "external_model", 8005, "批准绑定模拟，真实外部调用关闭"),
    _item("local_model", "本地模型接口", "legacy", "local_model", "local_model", 8006, "摘录模拟或受限Ollama"),
    _item("rag_green", "绿区检索", "legacy", "rag_green", "rag_green", 8007, "固定公开语料词法检索"),
    _item("rag_red", "红区检索", "legacy", "rag_red", "rag_red", 8008, "仅完全合成数据"),
    _item("redline_gate", "红区门禁", "legacy", "redline_gate", "redline_gate", 8009, "拒绝发布，人工释放未实现"),
    _item("output_verify", "输出核验", "legacy", "output_verify", "output_verify", 8010, "引文及有限规则，非医疗质量验收"),
    _item("daobrain_tuned", "专用推理", "legacy", "daobrain_tuned", "daobrain_tuned", 8011, "合成摘录，未训练模型"),
    _item("corpus_kb", "公开语料库", "legacy", "corpus_kb", "corpus_kb", 8012, "固定公开来源"),
    _item("card_annotator", "旧字卡标注", "legacy", "card_annotator", "card_annotator", 8013, "旧内存审核，重启丢失"),
    _item("ocr", "识别接口", "legacy", "ocr", "ocr", 8014, "文本分段，图片识别未实现"),
    _item("public_demo", "公开演示", "legacy", "public_demo", "public_demo", 8000, "三个固定样例"),
    _item("postgres", "PostgreSQL", "middleware", "postgres", "", None, "配置存在，业务未接入", False),
    _item("redis", "Redis", "middleware", "redis", "", None, "配置存在，分布式限额未接入", False),
    _item("qdrant", "Qdrant", "middleware", "qdrant", "", None, "配置存在，向量检索未接入", False),
    _item("ollama", "Ollama运行器", "model", "ollama", "", 11434, "需模型缓存与资源独立验收", False),
    _item("card_workflow", "字卡录入审核", "application", "backend", "card_workflow", 8000, "规范公开经典审核入库"),
    _item("knowledge_library", "典籍检索溯源", "application", "backend", "knowledge_library", 8000, "小语料词法检索"),
    _item("public_qa", "经义智能问答", "application", "backend", "public_qa", 8000, "真实小模型，语义质量未验收"),
    _item("clinical_records", "四诊记录辅助", "application", "backend", "clinical_records", 8000, "仅两个完全合成夹具"),
    _item("media_review", "图文视频解读", "application", "backend", "media_review", 8000, "仅公开引文文字，视觉未实现"),
    _item("membership", "门户会员支付", "application", "backend", "membership", 8000, "支付默认关闭，仅可选沙箱"),
    _item("frontend", "独立前端", "platform", "frontend", "portable_frontend", 8000, "固定后端同源代理", False),
    _item("local-node", "本地能力节点", "platform", "local-node", "local_node", 8000, "公开证据角色模拟，Windows未验收"),
    _item("offline-tests", "离线验收测试", "development", "", "tests", None, "可信测试不允许网页修改", False),
]
REGISTRY = {item["id"]: item for item in _COMPONENTS}
RUNTIMES = frozenset(item["runtime"] for item in _COMPONENTS if item["runtime"])
EDITABLE_DIRS = frozenset(item["directory"] for item in _COMPONENTS if item["editable"])
# Authentication and schema code are never editable from the developer workbench.
PROTECTED_DIRS = frozenset({"control_plane", "application_hub", "common", "tests", "scripts", "qa"})


class ControlError(ValueError):
    def __init__(self, detail, status=400):
        super().__init__(detail)
        self.detail, self.status = detail, status


def component(key):
    if not isinstance(key, str) or key not in REGISTRY:
        raise ControlError("组件未登记", 404)
    return dict(REGISTRY[key])


def validate_config(key, value):
    component(key)
    if not isinstance(value, dict) or set(value) != {"enabled", "request_timeout_s"}:
        raise ControlError("配置仅接受已登记字段")
    if type(value["enabled"]) is not bool or type(value["request_timeout_s"]) is not int:
        raise ControlError("配置字段类型无效")
    if not 1 <= value["request_timeout_s"] <= 120:
        raise ControlError("配置超时须为1至120秒")
    return dict(value)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()
