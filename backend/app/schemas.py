from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


ContentType = Literal[
    "short_video",
    "xiaohongshu",
    "wechat_article",
    "remix",
    "ad",
    "live_script",
    "moments",
    "sales_reply",
    "community",
]


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=300)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class BrandProfileUpdate(BaseModel):
    fields: dict[str, Any]


class InterviewAnswer(BaseModel):
    field: str
    answer: Any = None
    skip: bool = False


class VoiceSampleCreate(BaseModel):
    label: str = Field(default="表达样本", min_length=1, max_length=80)
    content: str = Field(min_length=20, max_length=30000)
    source_type: str = Field(default="paste", max_length=30)


class KnowledgeTextCreate(BaseModel):
    workspace_id: str
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    library_id: str | None = None
    source_type: str = "paste"
    is_gold: bool = False


class KnowledgeUrlCreate(BaseModel):
    workspace_id: str
    url: HttpUrl
    library_id: str | None = None
    is_gold: bool = False


class KnowledgeFolderCreate(BaseModel):
    workspace_id: str
    path: str
    library_id: str | None = None


class RetrievalRequest(BaseModel):
    workspace_id: str
    query: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1, le=30)
    gold_only: bool = False


class CopyTaskCreate(BaseModel):
    workspace_id: str
    content_type: ContentType
    platform: str = Field(min_length=1, max_length=40)
    topic: str = Field(min_length=2, max_length=500)
    goal: str = Field(default="获取精准咨询", min_length=1, max_length=300)
    offer: str = Field(default="", max_length=1000)
    constraints: dict[str, Any] = Field(default_factory=dict)
    source_urls: list[HttpUrl] = Field(default_factory=list, max_length=10)
    web_research: bool = True


class LifeCaseUpdate(BaseModel):
    archived: bool


class DraftEdit(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1)
    cta: str = ""
    alternative_titles: list[str] = Field(default_factory=list)
    platform_variants: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    deliverables: dict[str, Any] = Field(default_factory=dict)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    image_briefs: list[dict[str, Any]] = Field(default_factory=list)
    conversion_structure: dict[str, str] = Field(default_factory=dict)


class PerformanceCreate(BaseModel):
    workspace_id: str
    task_id: str | None = None
    draft_version_id: str | None = None
    platform: str
    impressions: int = Field(default=0, ge=0)
    reads: int = Field(default=0, ge=0)
    completions: int = Field(default=0, ge=0)
    interactions: int = Field(default=0, ge=0)
    inquiries: int = Field(default=0, ge=0)
    contacts: int = Field(default=0, ge=0)
    sales: int = Field(default=0, ge=0)
    revenue: float = Field(default=0, ge=0)
    notes: str = Field(default="", max_length=1000)


class ProfileCreate(BaseModel):
    workspace_id: str | None = None
    name: str
    provider: str
    protocol: Literal["chat_completions"] = "chat_completions"
    base_url: str
    model: str
    api_key: str | None = None
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=6000, ge=256, le=64000)
    is_default: bool = False

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        clean = value.rstrip("/")
        if not (clean.startswith("https://") or clean.startswith("http://127.0.0.1") or clean.startswith("http://localhost")):
            raise ValueError("远程模型地址必须使用 HTTPS；本机地址可使用 HTTP")
        return clean


class LocalProxySettingsUpdate(BaseModel):
    base_url: str = Field(min_length=1, max_length=300)
    model: str = Field(min_length=1, max_length=100)


class DeepSeekSettingsUpdate(BaseModel):
    """Direct DeepSeek settings.  The endpoint stays fixed on the server."""
    api_key: str | None = Field(default=None, max_length=1000)
    model: Literal["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"] = "deepseek-v4-flash"

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip()
        if "\n" in clean or "\r" in clean:
            raise ValueError("DeepSeek API Key 格式不正确")
        return clean or None


class SearchProfileCreate(BaseModel):
    workspace_id: str | None = None
    name: str
    provider: Literal["rss", "tavily", "exa", "serper"]
    endpoint: str = ""
    api_key: str | None = None
    is_default: bool = False


class ImageProfileCreate(BaseModel):
    workspace_id: str | None = None
    name: str
    provider: str = "openai-compatible"
    base_url: str
    model: str
    api_key: str | None = None
    is_default: bool = False

    @field_validator("base_url")
    @classmethod
    def validate_image_base_url(cls, value: str) -> str:
        clean = value.rstrip("/")
        if not (clean.startswith("https://") or clean.startswith("http://127.0.0.1") or clean.startswith("http://localhost")):
            raise ValueError("远程图片模型地址必须使用 HTTPS")
        return clean


class RoleUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    model_profile_id: str | None = None
    enabled: bool | None = None


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    steps: list[Literal["researcher", "writer", "editor"]] | None = None
    max_revisions: int | None = Field(default=None, ge=0, le=5)
    pass_score: float | None = Field(default=None, ge=0, le=100)


class ConnectorCreate(BaseModel):
    workspace_id: str
    platform: Literal["wechat", "xiaohongshu"]
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    secret: str | None = None


class TopicMonitorUpdate(BaseModel):
    enabled: bool | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=168)


class DailyIpRadarEnable(BaseModel):
    workspace_id: str
    interval_hours: int = Field(default=24, ge=1, le=168)


class PlatformBrowserOpen(BaseModel):
    platform: Literal["xiaohongshu", "douyin", "both"] = "both"
    query: str = Field(default="相亲", max_length=180)


class TopicDiscoveryRequest(BaseModel):
    workspace_id: str
    topic: str = Field(min_length=2, max_length=180)
    limit: int = Field(default=8, ge=1, le=12)


class XiaohongshuExtractionRequest(BaseModel):
    workspace_id: str
    url: HttpUrl
    search_keyword: str = Field(default="", max_length=180)
    title_hint: str = Field(default="", max_length=500)
    force: bool = False


class TopicDiscoveryCreate(BaseModel):
    """Turn selected public-search sources into a normal copy-workflow task."""

    workspace_id: str
    topic: str = Field(min_length=2, max_length=500)
    content_type: ContentType = "xiaohongshu"
    platform: str = Field(default="", max_length=40)
    product_mode: Literal["xiaohongshu_ip", "douyin_emotion"] | None = None
    goal: str = Field(default="根据公开研究生成可发布内容", min_length=1, max_length=300)
    offer: str = Field(default="", max_length=1000)
    constraints: dict[str, Any] = Field(default_factory=dict)
    source_urls: list[HttpUrl] = Field(default_factory=list, max_length=10)


class PublishApprove(BaseModel):
    workspace_id: str
    task_id: str
    draft_version_id: str
    connector_id: str
    scheduled_at: str | None = None


class PluginInvoke(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
