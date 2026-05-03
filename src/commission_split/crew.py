"""Crew assembly.

LLM agent (CrewAI):
  - parse_rule      — NLP extraction of percentage levels from rule text

Pure Python (deterministic, no LLM):
  - match_product   — exact case-insensitive lookup; returns matched=False if absent
  - build_hierarchy — recursive chain walk + AGENCY prepend + level-descending sort
  - calculate_commission — level_index lookup + roll-up + arithmetic
"""

import json
import logging

from crewai import Crew, Process

from commission_split.schemas import (
    AgentHierarchy,
    CommissionResult,
    CommissionSplit,
    HierarchyMember,
    ParsedRule,
    ProductMatch,
)
from commission_split.tasks import make_rule_parser_task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract(result: object, model_cls):
    """Pull typed output from CrewOutput via pydantic, json_dict, or raw."""
    if hasattr(result, "pydantic") and isinstance(result.pydantic, model_cls):
        return result.pydantic
    if hasattr(result, "json_dict") and result.json_dict:
        try:
            return model_cls(**result.json_dict)
        except Exception:
            pass
    if hasattr(result, "raw") and result.raw:
        raw = result.raw.strip()
        # Strip markdown code fences if the LLM wrapped the JSON
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(
                ln for ln in lines if not ln.strip().startswith("```")
            ).strip()
        try:
            return model_cls(**json.loads(raw))
        except Exception:
            pass
    raise ValueError(f"Cannot extract {model_cls.__name__} from {type(result)}: {result}")


def _run_single(task) -> object:
    crew = Crew(agents=[task.agent], tasks=[task], process=Process.sequential, verbose=True)
    return crew.kickoff()


def _is_null_id(value) -> bool:
    """Return True when a reporting_id means 'no manager'."""
    if value is None:
        return True
    s = str(value).strip().lower()
    return s in {"", "-", "none", "null", "nan"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def match_product(product_name: str, products_data: list[dict]) -> ProductMatch:
    """Pure Python: case-insensitive exact match against product catalog.

    Returns matched=False (no API call, no fuzzy guess) when the product name
    is not found — so unrecognised statement rows are skipped cleanly.
    """
    target = product_name.strip().lower()
    for p in products_data:
        catalog_name = str(p.get("product", "")).strip().lower()
        if catalog_name == target:
            # Accept either 'rule_name' (normalised Excel column) or 'rule'
            rule_text = str(p.get("rule_name", p.get("rule", ""))).strip()
            return ProductMatch(
                product_name=str(p["product"]),
                product_id=int(p["id"]),
                rule=rule_text,
                matched=True,
            )
    logger.warning("No product match for '%s' — skipping row.", product_name)
    return ProductMatch(product_name=product_name, product_id=-1, rule="", matched=False)


def build_hierarchy(agent_id: int, agents_data: list[dict], max_rule_level: int) -> AgentHierarchy:
    """Pure Python: walk the reporting chain, assign levels top-anchored, sort.

    Top-anchored level assignment (levels are FIXED by org position, not by who
    happens to be the selling agent in a given statement):

      chain length 3, max_rule_level=2:  HARINEE=0, THILAK=1, PRAVEEN=2
      chain length 2, max_rule_level=2:  THILAK=1,  PRAVEEN=2   (level 0 absent)
      chain length 1, max_rule_level=2:  PRAVEEN=2              (levels 0,1 absent)

    This ensures that when a lower-level agent is missing, `calculate_commission`
    sees the correct level_index for each present agent and roll-up fires properly.
    """
    lookup: dict[int, dict] = {}
    for a in agents_data:
        try:
            lookup[int(a["id"])] = a
        except (KeyError, ValueError, TypeError):
            continue

    # Collect the chain bottom-to-top: chain[0]=selling agent, chain[-1]=topmost
    chain: list[tuple[int, str]] = []  # (agent_id, name)
    current_id: int | None = agent_id
    visited: set[int] = set()

    while current_id is not None:
        if current_id in visited:
            logger.warning("Cycle detected in reporting chain at agent_id=%s", current_id)
            break
        visited.add(current_id)

        agent = lookup.get(current_id)
        if agent is None:
            logger.warning("Agent id=%s not found in agents data; skipping rest of chain", current_id)
            break

        chain.append((current_id, str(agent.get("name", f"Agent{current_id}")).strip().upper()))

        reporting_raw = agent.get("reporting_id")
        if _is_null_id(reporting_raw):
            break
        try:
            current_id = int(float(str(reporting_raw)))
        except (ValueError, TypeError):
            break

    chain_len = len(chain)
    non_agency_rule_count = max_rule_level + 1  # number of distinct non-agency levels in rule

    members: list[HierarchyMember] = []

    if chain_len <= non_agency_rule_count:
        # TOP-ANCHOR: chain is shorter than or equal to the rule's depth.
        # The chain is "missing" some lower levels; roll-up in calculate_commission
        # will redistribute their percentages upward.
        # Topmost agent → max_rule_level, counting down toward 0.
        #
        #  chain 3, max=2:  HARINEE=0  THILAK=1  PRAVEEN=2      (full match)
        #  chain 2, max=2:             THILAK=1  PRAVEEN=2      (level 0 absent → rollup)
        #  chain 1, max=2:                       PRAVEEN=2      (levels 0,1 absent → rollup)
        for pos, (aid, name) in enumerate(chain):
            level_index = max_rule_level - (chain_len - 1 - pos)
            members.append(HierarchyMember(agent_id=aid, name=name, level_index=level_index))
    else:
        # SUBAGENT: chain is longer than the rule's non-agency depth.
        # The top non_agency_rule_count people get top-anchored rule levels.
        # The bottom (chain_len - non_agency_rule_count) people are subagents — each
        # receives 50% of the person directly above them in the chain after the normal
        # split is computed (handled in calculate_commission).
        #
        #  chain 4, max=2:  KAVYA=subagent-of-HARINEE  HARINEE=0  THILAK=1  PRAVEEN=2
        #  chain 5, max=2:  KAVYA2=sub-of-KAVYA  KAVYA=sub-of-HARINEE  HARINEE=0  THILAK=1  PRAVEEN=2
        num_subagents = chain_len - non_agency_rule_count

        # Top chain people get top-anchored levels (same formula as TOP-ANCHOR)
        top_chain = chain[num_subagents:]
        for pos, (aid, name) in enumerate(top_chain):
            level_index = max_rule_level - (len(top_chain) - 1 - pos)
            members.append(HierarchyMember(agent_id=aid, name=name, level_index=level_index))

        # Subagents added topmost-first so stable sort keeps them in correct processing order
        for k in range(num_subagents - 1, -1, -1):
            sub_id, sub_name = chain[k]
            steals_from_id = chain[k + 1][0]   # agent_id of the person directly above
            members.append(HierarchyMember(
                agent_id=sub_id, name=sub_name, level_index=-2,
                is_subagent=True, subagent_of=steals_from_id,
            ))

    # Add AGENCY as the fixed top node (level -1)
    members.append(HierarchyMember(agent_id=None, name="AGENCY", level_index=-1))

    # Sort: AGENCY first, then descending level_index (highest manager → selling agent),
    # subagents last (in topmost-first insertion order, preserved by stable sort)
    def _sort_key(m: HierarchyMember) -> tuple[int, int]:
        if m.level_index == -1:
            return (0, 0)
        if m.is_subagent:
            return (2, 0)
        return (1, -m.level_index)

    members.sort(key=_sort_key)
    return AgentHierarchy(members=members)


def parse_rule(rule_text: str) -> ParsedRule:
    """LLM agent: natural-language rule → ParsedRule with level_index percentages."""
    task = make_rule_parser_task(rule_text)
    result = _run_single(task)
    return _extract(result, ParsedRule)


def _resolve_percentages_with_rollup(
    rule: ParsedRule,
    hierarchy_levels: set[int],
) -> dict[int, float]:
    """Return {level_index: final_percentage} with roll-up applied for missing levels.

    Roll-up rule (non-agency levels only):
      For each rule level whose level_index is absent from hierarchy_levels, find the
      nearest higher level that IS present (smallest level_index > missing one among
      non-agency hierarchy members).  If no higher level exists, use the highest
      available non-agency level.  Add the orphaned percentage there.

    AGENCY (level -1) is always present; its percentage is never rolled anywhere.
    """
    # Initialise every present level at 0
    final: dict[int, float] = {li: 0.0 for li in hierarchy_levels}

    # Non-agency levels present in hierarchy, sorted ascending
    non_agency = sorted(li for li in hierarchy_levels if li != -1)

    for rule_lvl in rule.levels:
        li = rule_lvl.level_index
        pct = rule_lvl.percentage

        if li in hierarchy_levels:
            # Level is present — assign directly
            final[li] += pct
        elif li == -1:
            # Agency rule entry but agency is somehow absent — shouldn't happen,
            # but add to the highest non-agency level as a safe fallback
            target = max(non_agency) if non_agency else None
            if target is not None:
                final[target] += pct
                logger.warning("Agency level absent from hierarchy; rolled %.1f%% to level %d", pct, target)
        else:
            # Missing non-agency level — roll up to the nearest higher level
            higher = [l for l in non_agency if l > li]
            if higher:
                target = min(higher)          # nearest higher present level
            elif non_agency:
                target = max(non_agency)      # no higher exists; use the highest available
            else:
                target = -1                   # only AGENCY present; roll into it
            final[target] += pct
            logger.info("Level %d absent; rolled %.1f%% up to level %d", li, pct, target)

    return final


def calculate_commission(
    hierarchy: AgentHierarchy,
    rule: ParsedRule,
    total_commission: float,
    product: str,
    policy_number: str,
) -> CommissionResult:
    """Pure Python: roll-up missing levels, compute amounts, then apply subagent 50% split.

    Subagent rule: when a chain member has is_subagent=True they receive exactly 50% of
    the amount assigned to the person they report to (subagent_of), and that person keeps
    the remaining 50%.  Cascading subagents are processed topmost-first.
    """
    normal_members = [m for m in hierarchy.members if not m.is_subagent]

    hierarchy_levels = {m.level_index for m in normal_members}
    final_pct = _resolve_percentages_with_rollup(rule, hierarchy_levels)

    # Build mutable CommissionSplit objects for normal members, keyed by agent_id
    id_to_split: dict[int | None, CommissionSplit] = {}
    for member in normal_members:
        percentage = final_pct.get(member.level_index, 0.0)
        amount = round(total_commission * (percentage / 100), 2)
        s = CommissionSplit(
            name=member.name, level_index=member.level_index,
            percentage=percentage, amount=amount,
        )
        id_to_split[member.agent_id] = s

    # Apply subagent 50% steal, topmost subagent first (order preserved from build_hierarchy)
    sub_id_to_split: dict[int, CommissionSplit] = {}
    for member in hierarchy.members:
        if not member.is_subagent:
            continue
        # Look up target in normal members first, then already-processed subagents (cascading)
        target: CommissionSplit | None = (
            id_to_split.get(member.subagent_of) or sub_id_to_split.get(member.subagent_of)
        )
        if target is None:
            sub_id_to_split[member.agent_id] = CommissionSplit(
                name=member.name, level_index=-2, percentage=0.0, amount=0.0)
            continue
        stolen_pct = round(target.percentage / 2, 4)
        stolen_amt = round(total_commission * stolen_pct / 100, 2)
        target.percentage = stolen_pct
        target.amount = stolen_amt
        sub_id_to_split[member.agent_id] = CommissionSplit(
            name=member.name, level_index=-2, percentage=stolen_pct, amount=stolen_amt)

    # Collect splits in hierarchy display order
    splits: list[CommissionSplit] = []
    for member in hierarchy.members:
        if member.is_subagent:
            if member.agent_id in sub_id_to_split:
                splits.append(sub_id_to_split[member.agent_id])
        else:
            if member.agent_id in id_to_split:
                splits.append(id_to_split[member.agent_id])

    return CommissionResult(
        product=product,
        policy_number=policy_number,
        total_commission=total_commission,
        splits=splits,
        rule_reasoning=rule.reasoning,
        rule_confidence=rule.confidence,
    )
