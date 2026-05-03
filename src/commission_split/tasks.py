"""CrewAI task definitions — rule parsing only.
Product matching, hierarchy building, and commission calculation are pure Python in crew.py."""

from crewai import Task

from commission_split.agents import create_rule_parser_agent, get_llm
from commission_split.schemas import ParsedRule


def make_rule_parser_task(rule_text: str) -> Task:
    agent = create_rule_parser_agent(get_llm())
    return Task(
        description=f"""Parse the commission rule below into structured percentage levels.

Rule: "{rule_text}"

Level index mapping (strictly follow this):
  "agent"                              → level_index = 0
  "reporting manager" / "his ... manager" → level_index = 1
  "reporting manager's manager"        → level_index = 2
  any further manager up the chain     → level_index = 3, 4, …
  "agency"                             → level_index = -1

Return a JSON object with these keys:
  - "levels"     → array of objects, each with:
                     level_index  (integer, use -1 for agency)
                     label        (short human-readable string)
                     percentage   (number, 0–100)
  - "reasoning"  → one sentence explaining how you identified each level and percentage
  - "confidence" → float 0.0–1.0 reflecting how clearly the rule stated the values
                   (1.0 = perfectly explicit, 0.5 = some ambiguity, <0.5 = guessed)

Rules:
  • Extract only what is stated — never invent percentages.
  • All percentages must sum to exactly 100.
  • Do not include any level not mentioned in the rule.

Output raw JSON only. No markdown, no code fences, no explanation.""",
        expected_output='Raw JSON with "levels" array, "reasoning" string, and "confidence" float.',
        agent=agent,
        output_pydantic=ParsedRule,
    )
