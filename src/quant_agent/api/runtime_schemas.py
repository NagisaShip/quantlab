"""Shared validated human reconciliation requests for Runtime hosts."""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class EffectResolutionIn(BaseModel):
    step_id: str = Field(min_length=1, max_length=200)
    signal_id: str = Field(min_length=1, max_length=200)
    outcome: Literal["succeeded", "absent"]
    result: Any = None


class ModelUsageResolutionIn(BaseModel):
    step_id: str = Field(min_length=1, max_length=200)
    usage: dict[str, Any] | None = None
    no_charge_confirmed: bool = False

    @field_validator("usage")
    @classmethod
    def confirmed_usage(cls, value):
        if value is None:
            return value
        counters = {
            "input_tokens",
            "output_tokens",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "prompt_cache_hit_tokens",
        }

        def validate(items):
            found = False
            for key, count in items.items():
                if isinstance(count, dict):
                    found = validate(count) or found
                elif key in counters:
                    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                        raise ValueError("已确认的 token 用量必须是非负整数")
                    found = True
            return found

        if not validate(value):
            raise ValueError("缺少已确认的 token 用量")
        return value
