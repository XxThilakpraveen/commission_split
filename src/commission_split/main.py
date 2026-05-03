"""Entry point: read Excel files, run the commission split pipeline for each statement row."""

import json
import logging
import os
import sys

import pandas as pd
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from commission_split import crew

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

console = Console()


def _load_excel(path: str, label: str) -> pd.DataFrame:
    if not os.path.exists(path):
        console.print(f"[red]Error:[/red] {label} file not found at '{path}'")
        sys.exit(1)
    return pd.read_excel(path)


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    return df


def _agents_to_records(agents_df: pd.DataFrame) -> list[dict]:
    records = []
    for _, row in agents_df.iterrows():
        rid = row.get("reporting_id")
        if pd.isna(rid) or str(rid).strip() in {"-", ""}:
            rid = None
        else:
            try:
                rid = int(float(str(rid)))
            except (ValueError, TypeError):
                rid = None
        records.append({
            "id": int(row["id"]),
            "name": str(row["name"]).strip(),
            "reporting_id": rid,
        })
    return records


def _confidence_str(confidence: float) -> str:
    pct = f"{confidence:.0%}"
    if confidence >= 0.9:
        return f"[bold green]{pct}[/bold green]"
    elif confidence >= 0.7:
        return f"[bold yellow]{pct}[/bold yellow]"
    return f"[bold red]{pct}[/bold red]"


def _print_combined_table(all_results: list[dict]) -> None:
    """Print the commission split table followed by a rule reasoning section."""

    # ── Split table (compact — no reasoning column) ───────────────────────────
    table = Table(show_header=True, header_style="bold magenta", show_lines=True,
                  expand=False)
    table.add_column("Product",  style="bold cyan", no_wrap=True, min_width=12)
    table.add_column("Policy",   style="dim",       no_wrap=True)
    table.add_column("Comm $",   justify="right",   style="dim",       no_wrap=True)
    table.add_column("Name",     style="bold",      no_wrap=True, min_width=8)
    table.add_column("Lvl",      justify="right")
    table.add_column("%",        justify="right",   no_wrap=True)
    table.add_column("Amount",   justify="right",   style="bold green", no_wrap=True)
    table.add_column("Conf",     justify="center",  no_wrap=True)

    for result in all_results:
        splits     = result["splits"]
        total      = result["total_commission"]
        confidence = result.get("rule_confidence", 1.0)
        conf_cell  = _confidence_str(confidence)

        is_error = any(s["level_index"] == -99 for s in splits)
        for i, split in enumerate(splits):
            name_cell = (
                f"[bold red]{split['name']}[/bold red]" if is_error else split["name"]
            )
            lvl_cell = (
                "—"   if split["level_index"] == -99 else
                "Sub" if split["level_index"] == -2  else
                str(split["level_index"])
            )
            pct_cell = "—" if is_error else f"{split['percentage']:.1f}%"
            amt_cell = "[bold red]PARSE ERROR[/bold red]" if is_error else f"${split['amount']:,.2f}"
            table.add_row(
                result["product"]       if i == 0 else "",
                result["policy_number"] if i == 0 else "",
                f"${total:,.2f}"        if i == 0 else "",
                name_cell,
                lvl_cell,
                pct_cell,
                amt_cell,
                conf_cell               if i == 0 else "",
            )

    console.print()
    console.rule("[bold magenta]Commission Split Summary[/bold magenta]")
    console.print(table)

    # ── Reasoning section (one block per statement) ───────────────────────────
    console.print()
    console.rule("[bold blue]Rule Reasoning[/bold blue]")
    for result in all_results:
        reasoning  = result.get("rule_reasoning", "").strip()
        confidence = result.get("rule_confidence", 1.0)
        if not reasoning:
            continue
        console.print(
            f"  [bold cyan]{result['product']}[/bold cyan]  "
            f"[dim]{result['policy_number']}[/dim]  "
            f"Confidence: {_confidence_str(confidence)}"
        )
        console.print(f"  [italic white]{reasoning}[/italic white]")
        console.print()


def main() -> None:
    products_path  = os.getenv("PRODUCTS_FILE",  "data/products.xlsx")
    agents_path    = os.getenv("AGENTS_FILE",    "data/agents.xlsx")
    statement_path = os.getenv("STATEMENT_FILE", "data/statement.xlsx")

    products_df  = _normalize_columns(_load_excel(products_path,  "Products"))
    agents_df    = _normalize_columns(_load_excel(agents_path,    "Agents"))
    statement_df = _normalize_columns(_load_excel(statement_path, "Statement"))

    products_data  = products_df.to_dict(orient="records")
    agents_records = _agents_to_records(agents_df)

    all_results: list[dict] = []
    skipped = 0

    for idx, row in statement_df.iterrows():
        product_name  = str(row.get("product",    "")).strip()
        policy_number = str(row.get("policy",     "")).strip()
        commission    = float(row.get("commission", 0))
        agent_id      = int(row.get("agent_id",    0))

        console.rule(f"[bold green]Row {idx + 1}: {product_name} | {policy_number}[/bold green]")

        # Step 1: exact product lookup
        console.print("[yellow]Step 1:[/yellow] Matching product…")
        product_match = crew.match_product(product_name, products_data)
        if not product_match.matched:
            console.print(
                f"  [bold red]No match:[/bold red] '{product_name}' not in catalog — skipping."
            )
            skipped += 1
            continue
        console.print(f"  → [bold]{product_match.product_name}[/bold]  Rule: {product_match.rule}")

        # Step 2: parse rule (LLM)
        console.print("[yellow]Step 2:[/yellow] Parsing rule…")
        try:
            parsed_rule = crew.parse_rule(product_match.rule)
        except Exception as exc:
            console.print(f"  [bold red]Rule parse failed:[/bold red] {exc} — recording as low-confidence.")
            all_results.append({
                "product": product_name,
                "policy_number": policy_number,
                "total_commission": commission,
                "splits": [{
                    "name": "UNRESOLVED",
                    "level_index": -99,
                    "percentage": 0.0,
                    "amount": 0.0,
                }],
                "rule_reasoning": f"Rule could not be parsed by LLM: {exc}",
                "rule_confidence": 0.0,
            })
            continue

        non_agency_levels = [l.level_index for l in parsed_rule.levels if l.level_index != -1]
        if not non_agency_levels:
            console.print("  [bold red]No agent levels found in rule[/bold red] — agency-only rule or parse error.")
            all_results.append({
                "product": product_name,
                "policy_number": policy_number,
                "total_commission": commission,
                "splits": [{
                    "name": "UNRESOLVED",
                    "level_index": -99,
                    "percentage": 0.0,
                    "amount": 0.0,
                }],
                "rule_reasoning": parsed_rule.reasoning or "No non-agency levels found in parsed rule.",
                "rule_confidence": min(parsed_rule.confidence, 0.3),
            })
            continue

        max_rule_level = max(non_agency_levels)
        for lvl in parsed_rule.levels:
            console.print(f"  level {lvl.level_index:>2} ({lvl.label}): {lvl.percentage}%")

        # Step 3: build hierarchy
        console.print("[yellow]Step 3:[/yellow] Building hierarchy…")
        hierarchy = crew.build_hierarchy(agent_id, agents_records, max_rule_level)
        console.print("  " + " → ".join(f"{m.name}({m.level_index})" for m in hierarchy.members))

        # Step 4 & 5: calculate splits
        console.print("[yellow]Step 4 & 5:[/yellow] Calculating splits…")
        result = crew.calculate_commission(
            hierarchy, parsed_rule, commission, product_name, policy_number
        )
        all_results.append(result.model_dump())

    # ── Combined summary table ────────────────────────────────────────────────
    if all_results:
        _print_combined_table(all_results)

    # ── Save JSON ─────────────────────────────────────────────────────────────
    output_path = "commission_results.json"
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2)

    console.print(
        f"\n[bold green]Done.[/bold green] {len(all_results)} processed, "
        f"{skipped} skipped. Results saved to [bold]{output_path}[/bold]\n"
    )


if __name__ == "__main__":
    main()
