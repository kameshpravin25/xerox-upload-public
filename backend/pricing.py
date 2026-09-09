"""
Server-Side Pricing Calculator

Mirrors the client-side CostEstimator logic exactly.
Used to recalculate the accurate price on the server after
office documents are converted to PDF and pages are counted.

This is the SOURCE OF TRUTH for billing — the client estimate
is only a preview.
"""

import json
import math
import logging
from pathlib import Path
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Cache the pricing config
_pricing_config = None


def _load_pricing() -> dict:
    """Load pricing configuration from pricing.json."""
    global _pricing_config
    if _pricing_config is not None:
        return _pricing_config

    pricing_path = Path(__file__).parent.parent / "frontend" / "pricing.json"
    try:
        with open(pricing_path, "r") as f:
            _pricing_config = json.load(f)
        logger.info("Loaded pricing config from pricing.json")
    except Exception as e:
        logger.error(f"Failed to load pricing.json: {e}")
        # Fallback defaults matching the client
        _pricing_config = {
            "base_prices": {
                "bw_per_page_cents": 180,
                "bw_duplex_per_page_cents": 200,
                "color_per_page_cents": 500,
                "color_duplex_per_page_cents": 1000,
            },
            "paper_sizes": {"A4": {"multiplier": 1.0}},
            "binding": {"none": {"price_cents": 0}},
            "copies": {"bulk_discount": {"threshold": 10, "discount_percent": 10}},
            "minimum_job_cents": 180,
        }
    return _pricing_config


def calculate_price_cents(
    page_count: int,
    color_mode: str = "bw",
    duplex: bool = False,
    paper_size: str = "A4",
    binding: str = "none",
    copies: int = 1,
) -> int:
    """
    Calculate the total cost in cents (paise).

    Mirrors the client-side CostEstimator.calculateCostCents() exactly.

    Args:
        page_count: Actual number of pages
        color_mode: 'bw' or 'color'
        duplex: True for double-sided
        paper_size: 'A4', 'A3', 'Letter', 'Legal'
        binding: 'none', 'staple', 'spiral', 'hardcover'
        copies: Number of copies

    Returns:
        Total cost in cents (paise)
    """
    pricing = _load_pricing()

    if page_count <= 0:
        return 0

    # Base price per page (explicit duplex pricing)
    if color_mode == "color":
        base_price = (
            pricing["base_prices"]["color_duplex_per_page_cents"]
            if duplex
            else pricing["base_prices"]["color_per_page_cents"]
        )
    else:
        base_price = (
            pricing["base_prices"]["bw_duplex_per_page_cents"]
            if duplex
            else pricing["base_prices"]["bw_per_page_cents"]
        )

    # Paper size multiplier
    paper_config = pricing.get("paper_sizes", {}).get(paper_size, {})
    paper_multiplier = paper_config.get("multiplier", 1.0)

    # Price per unit (per page for single, per sheet for duplex)
    price_per_unit = base_price * paper_multiplier

    # For duplex, calculate based on sheets (physical papers) needed
    # Each sheet holds 2 pages, so sheets = ceil(pages / 2)
    if duplex:
        billable_units = math.ceil(page_count / 2)
    else:
        billable_units = page_count

    # Total cost
    total_cents = price_per_unit * billable_units

    # Multiply by copies
    total_cents *= copies

    # Bulk discount
    bulk_discount = pricing.get("copies", {}).get("bulk_discount", {})
    if bulk_discount and copies >= bulk_discount.get("threshold", 10):
        total_cents *= 1 - bulk_discount.get("discount_percent", 10) / 100

    # Binding cost
    binding_config = pricing.get("binding", {}).get(binding, {})
    binding_cost = binding_config.get("price_cents", 0)
    total_cents += binding_cost * copies

    # Minimum job charge
    minimum = pricing.get("minimum_job_cents", 180)
    if total_cents < minimum:
        total_cents = minimum

    return round(total_cents)
