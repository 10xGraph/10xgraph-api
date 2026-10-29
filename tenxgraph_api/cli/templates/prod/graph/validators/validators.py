from tenxgraph.utils.validators import PromptInjectionValidator


prompt_validator = PromptInjectionValidator(
    strict_mode=True,
    max_length=10000,
    blocked_patterns=[],
    suspicious_keywords=[
        "ignore previous",
        "forget previous",
        "disregard previous",
        "circumvent",
        "remove restrictions",
        "coupon",
    ],
)
