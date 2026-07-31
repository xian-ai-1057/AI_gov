"""Central prompt-management boundary."""

from govcheck.prompts.catalog import (
    PromptCatalog,
    PromptContract,
    profile_for_item,
    short_name_for_item,
)
from govcheck.prompts.gateway import PromptGateway, PromptRun, build_prompt_gateway
from govcheck.prompts.rubrics import load_rubrics, rubric_path
from govcheck.prompts.providers import (
    FilePromptProvider,
    LiteLLMPromptProvider,
    NullPromptProvider,
    PromptProvider,
    PromptTemplate,
    PromptUnavailable,
    StaticPromptProvider,
)

__all__ = [
    "FilePromptProvider",
    "LiteLLMPromptProvider",
    "NullPromptProvider",
    "PromptCatalog",
    "PromptContract",
    "PromptGateway",
    "PromptProvider",
    "PromptRun",
    "PromptTemplate",
    "PromptUnavailable",
    "StaticPromptProvider",
    "build_prompt_gateway",
    "load_rubrics",
    "profile_for_item",
    "rubric_path",
    "short_name_for_item",
]
