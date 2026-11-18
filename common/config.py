"""集中配置：从环境变量读取（pydantic-settings）。各服务共用。"""
from functools import lru_cache
from decimal import Decimal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore",
                                      protected_namespaces=("model_validate", "model_dump"))

    daobrain_env: str = "dev"
    log_level: str = "INFO"

    postgres_dsn: str = "postgresql+psycopg://daobrain:daobrain@postgres:5432/daobrain"
    redis_url: str = "redis://redis:6379/0"
    qdrant_url: str = "http://qdrant:6333"

    # 模块间调用地址
    gateway_url: str = "http://gateway:8000"
    orchestrator_url: str = "http://agent_orchestrator:8002"
    rag_public_url: str = "http://rag_public:8003"
    hybrid_iface_url: str = "http://hybrid_iface:8004"
    external_model_url: str = "http://external_model:8005"
    local_model_url: str = "http://local_model:8006"
    rag_green_url: str = "http://rag_green:8007"
    rag_red_url: str = "http://rag_red:8008"
    redline_gate_url: str = "http://redline_gate:8009"
    output_verify_url: str = "http://output_verify:8010"
    daobrain_tuned_url: str = "http://daobrain_tuned:8011"
    corpus_kb_url: str = "http://corpus_kb:8012"
    card_annotator_url: str = "http://card_annotator:8013"
    ocr_url: str = "http://ocr:8014"

    # 红/绿区与外部模型
    external_model_mode: str = "stub"  # stub | live
    external_model_api_key: str = ""
    external_model_base_url: str = ""
    local_model_backend: str = "ollama"  # ollama | stub
    local_model_name: str = "qwen2.5:3b"
    # 两条绿区路径显式选择；CPU部署覆盖为local_ollama，失败不跨路径回退。
    gateway_green_backend: str = "external_stub"  # external_stub | local_ollama
    # copy保留严格原文复制协议；select由真实模型选证据ID，服务器核验后逐字摘录。
    model_output_protocol: str = "copy"  # copy | select
    model_context_tokens: int = Field(default=4096, ge=512, le=8192)
    model_cpu_threads: int = Field(default=2, ge=1, le=32)

    # 默认没有有效密钥，业务接口失败关闭；本地启动脚本生成临时密钥。
    api_key: str = ""
    internal_api_key: str = ""
    approval_secret: str = ""
    allow_external_stub: bool = True
    ollama_url: str = "http://ollama:11434"
    open_reason_model: str = "qwen2.5:0.5b"
    open_reason_enabled: bool = True
    model_timeout_seconds: int = Field(default=60, ge=1, le=120)
    model_max_output_tokens: int = Field(default=256, ge=16, le=512)
    max_requests_per_minute: int = Field(default=30, ge=1, le=1000)

    # 金额用 Decimal；计数为单进程模拟，分布式日预算尚不宣称已实现。
    max_tokens_per_task: int = Field(default=8000, ge=1)
    max_iterations_per_task: int = Field(default=6, ge=1, le=6)
    max_cost_cny_per_task: Decimal = Field(default=Decimal("0.50"), ge=0, allow_inf_nan=False)
    daily_budget_cny: Decimal = Field(default=Decimal("50.00"), ge=0, allow_inf_nan=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
