"""CrewAI agent definitions. Only product matching and rule parsing use LLM —
hierarchy traversal and commission math are handled deterministically in crew.py."""

import os

from crewai import Agent, LLM


def get_llm() -> LLM | None:
    model = os.getenv("MODEL", "gpt-4o-mini")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    return LLM(model=model, api_key=api_key)


def _agent(role: str, goal: str, backstory: str, llm: LLM | None = None) -> Agent:
    kwargs = {"llm": llm} if llm else {}
    return Agent(
        role=role,
        goal=goal,
        backstory=backstory,
        verbose=True,
        max_iter=5,
        max_execution_time=120,
        allow_delegation=False,
        **kwargs,
    )


def create_product_matcher_agent(llm: LLM | None = None) -> Agent:
    return _agent(
        role="Product matcher",
        goal=(
            "Find the product in the catalog that matches the given name "
            "and return its id and rule as JSON."
        ),
        backstory=(
            "You do exact or fuzzy case-insensitive name matching against a product catalog. "
            "You always return a JSON object with product_name, product_id, and rule. "
            "If no match exists, return the closest entry."
        ),
        llm=llm,
    )


def create_rule_parser_agent(llm: LLM | None = None) -> Agent:
    return _agent(
        role="Commission rule parser",
        goal=(
            "Parse a natural-language commission rule sentence into structured "
            "percentage levels indexed by hierarchy depth."
        ),
        backstory=(
            "You read sentences like 'agent gets 40%, his reporting manager gets 30%, "
            "his reporting manager's manager gets 20%, agency gets 10%' and convert them "
            "into a JSON list. "
            "Level indexing: agent=0, direct manager=1, manager's manager=2, each further "
            "manager increments by 1, agency=-1. "
            "You never invent percentages — only extract what is stated. "
            "Percentages must sum to 100."
        ),
        llm=llm,
    )
