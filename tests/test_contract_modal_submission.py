import ast
import inspect
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Gemini model configuration
# ---------------------------------------------------------------------------


def test_config_default_llm_model_is_gemini_3_8_flash():
    from config import load_settings

    # load_settings reads env; ensure no override
    import os

    old = os.environ.get("LLM_MODEL")
    try:
        os.environ.pop("LLM_MODEL", None)
        settings = load_settings()
        assert settings.llm_model == "gemini-3.8-flash"
    finally:
        if old is None:
            os.environ.pop("LLM_MODEL", None)
        else:
            os.environ["LLM_MODEL"] = old


def test_env_example_uses_gemini_3_8_flash():
    content = Path(".env.example").read_text(encoding="utf-8")
    assert "LLM_MODEL=gemini-3.8-flash" in content
    # obsolete model should not be present as an active config value
    assert "LLM_MODEL=gemini-2.5-flash" not in content


def test_llm_service_candidate_models_do_not_include_obsolete_2_5_flash():
    from config import Settings
    from services.llm_service import LLMService

    settings = Settings(
        discord_bot_token="",
        discord_guild_id=None,
        football_api_key="",
        football_api_base_url="https://example.invalid",
        football_api_season=2026,
        football_data_source="bundled",
        database_path=":memory:",
        command_prefix="!",
        log_level="INFO",
        match_tick_seconds=1.0,
        cache_ttl_hours=24,
        api_request_timeout=20,
        api_max_retries=3,
        llm_api_key="test-key",
        llm_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        llm_model="gemini-3.8-flash",
        llm_timeout=20,
    )
    svc = LLMService(settings)
    candidates = svc._candidate_models(None)
    assert candidates[0] == "gemini-3.8-flash"
    assert "gemini-2.5-flash" not in candidates
    # no duplicates
    assert len(candidates) == len(set(candidates))


def test_llm_service_candidate_models_preserves_explicit_model_and_falls_back():
    from config import Settings
    from services.llm_service import LLMService

    settings = Settings(
        discord_bot_token="",
        discord_guild_id=None,
        football_api_key="",
        football_api_base_url="https://example.invalid",
        football_api_season=2026,
        football_data_source="bundled",
        database_path=":memory:",
        command_prefix="!",
        log_level="INFO",
        match_tick_seconds=1.0,
        cache_ttl_hours=24,
        api_request_timeout=20,
        api_max_retries=3,
        llm_api_key="test-key",
        llm_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        llm_model="gemini-3.8-flash",
        llm_timeout=20,
    )
    svc = LLMService(settings)
    candidates = svc._candidate_models("gemini-3-flash-preview")
    assert candidates[0] == "gemini-3-flash-preview"
    assert "gemini-3.8-flash" in candidates
    assert "gemini-2.5-flash" not in candidates


# ---------------------------------------------------------------------------
# normalize_role regression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Crucial", "Crucial"),
        ("crucial", "Crucial"),
        ("IMPORTANT", "Important"),
        ("rotation", "Rotation"),
        ("sporadic", "Sporadic"),
        ("prospect", "Prospect"),
        ("star player", "Crucial"),
        ("starter", "Important"),
        ("squad player", "Rotation"),
        ("bench", "Sporadic"),
        ("youngster", "Prospect"),
    ],
)
def test_normalize_role_valid_inputs(raw, expected):
    from services.transfer_market_service import normalize_role

    assert normalize_role(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "not-a-role", "???", "123"])
def test_normalize_role_invalid_or_empty_returns_none(raw):
    from services.transfer_market_service import normalize_role

    assert normalize_role(raw) is None


def test_normalize_role_fallback_usage_is_safe():
    from services.transfer_market_service import normalize_role

    demand = {"role": None}
    role = normalize_role(str(demand.get("role") or "Rotation")) or "Rotation"
    assert role == "Rotation"

    role = normalize_role("") or "Rotation"
    assert role == "Rotation"

    role = normalize_role(None) or str(demand.get("role") or "Rotation")
    assert role == "Rotation"


# ---------------------------------------------------------------------------
# Static regression: no local normalize_role shadowing in contract modal handler
# ---------------------------------------------------------------------------


def test_handle_contract_modal_submit_has_no_local_normalize_role_import_or_assignment():
    src = Path("bot/cogs/transfers.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # Find the method
    method_node = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "handle_contract_modal_submit":
            method_node = node
            break
    assert method_node is not None, "handle_contract_modal_submit not found"

    # Check for local imports or assignments to normalize_role
    problems = []
    for node in ast.walk(method_node):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname == "normalize_role" or alias.name == "normalize_role":
                    problems.append(f"local import of normalize_role at line {node.lineno}")
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "normalize_role":
                    problems.append(f"local assignment to normalize_role at line {node.lineno}")
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == "normalize_role":
                problems.append(f"annotated assignment to normalize_role at line {node.lineno}")
    assert problems == [], "Found normalize_role shadowing: " + "; ".join(problems)


# ---------------------------------------------------------------------------
# Contract modal submission regression (UnboundLocalError / full flow)
# ---------------------------------------------------------------------------


def _make_interaction(user_id: int = 42):
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.guild_id = 1
    interaction.channel_id = 10
    interaction.response.is_done.return_value = False
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_modal(cog, offer_id: int, value: str):
    modal = MagicMock()
    modal.offer_id = offer_id
    modal.offer.value = value
    modal.cog = cog
    return modal


def _make_bot():
    bot = MagicMock()
    bot.db = MagicMock()
    bot.db.execute = AsyncMock()
    bot.db.fetchone = AsyncMock(return_value=None)
    bot.season_service = MagicMock()
    bot.season_service.get_season = AsyncMock(return_value={"id": "S1", "season_year": 2026, "league_key": "premier_league"})
    bot.season_service.create_pending_transfer = AsyncMock(return_value={"ok": True, "transfer": {"id": 99}})
    bot.season_service.confirm_transfer = AsyncMock(return_value={"ok": True, "player_name": "Test Player", "from_team_name": "Free Agents", "to_team_name": "Buying FC"})
    bot.season_service.ensure_season_budgets = AsyncMock()
    bot.football_data = MagicMock()
    bot.football_data.ensure_seeded = AsyncMock()

    llm = MagicMock()
    llm.enabled = True
    llm.last_error = None
    llm.negotiate_contract = AsyncMock(return_value={
        "accepted": False,
        "walked_away": False,
        "pressure": 2,
        "pressure_bar": "[Negotiation Pressure: ██░░░░░░░░ 20%]",
        "counter": {"wage": 150000, "role": "Important", "signing_bonus": 1000000, "contract_years": 3},
        "agreed_terms": None,
        "message": "[Negotiation Pressure: ██░░░░░░░░ 20%]\nCounter offer.",
    })
    llm.chat = AsyncMock(return_value="[Negotiation Pressure: ██░░░░░░░░ 20%]\nCounter offer.")
    llm._sanitize_contract_counter = lambda raw, fallback=None: (raw if isinstance(raw, dict) and raw.get("wage") else None)
    bot.llm_service = llm
    return bot


@pytest.mark.asyncio
async def test_contract_modal_submit_does_not_raise_unboundlocalerror():
    from bot.cogs.transfers import TransfersCog

    bot = _make_bot()
    cog = TransfersCog(bot)

    offer = {
        "id": 38,
        "season_id": "S1",
        "guild_id": 1,
        "channel_id": 10,
        "kind": "free_agent_contract",
        "status": "negotiating",
        "buying_user_id": 42,
        "buying_team_api_id": 10,
        "buying_team_name": "Buying FC",
        "selling_team_api_id": 9000001,
        "selling_team_name": "Free Agents",
        "selling_manager_user_id": None,
        "player_api_id": 12345,
        "player_name": "Test Player",
        "fee": 0,
        "stage": "contract",
        "meta_json": json.dumps({
            "demand": {"wage": 120000, "role": "Important", "signing_bonus": 500000, "contract_years": 3},
            "valuation": {"market_value": 10_000_000},
            "contract_round": 0,
            "pressure": 0,
            "is_free_agent": True,
        }),
    }
    cog.get_offer = AsyncMock(return_value=offer)
    cog.market.get_player = AsyncMock(return_value={
        "api_id": 12345, "name": "Test Player", "overall": 80, "potential": 85, "age": 27, "position": "ST",
    })
    cog._finalize_agreed_offer = AsyncMock(return_value={"ok": True, "message": "done"})

    interaction = _make_interaction(user_id=42)
    modal = _make_modal(cog, 38, "can we lower salary to like 100k")

    # The key regression: this used to raise UnboundLocalError for normalize_role
    await cog.handle_contract_modal_submit(interaction, modal)

    # Should have responded (followup or response) - no crash
    assert interaction.followup.send.await_count >= 1 or interaction.response.send_message.await_count >= 1


@pytest.mark.asyncio
async def test_contract_modal_submit_handles_missing_role_without_crash():
    from bot.cogs.transfers import TransfersCog

    bot = _make_bot()
    cog = TransfersCog(bot)
    offer = {
        "id": 39,
        "season_id": "S1",
        "guild_id": 1,
        "channel_id": 10,
        "kind": "contract_nego",
        "status": "negotiating",
        "buying_user_id": 42,
        "buying_team_api_id": 10,
        "buying_team_name": "Buying FC",
        "selling_team_api_id": 20,
        "selling_team_name": "Selling FC",
        "selling_manager_user_id": None,
        "player_api_id": 12346,
        "player_name": "Roleless Player",
        "fee": 1_000_000,
        "stage": "contract",
        "meta_json": json.dumps({
            "demand": {"wage": 120000, "role": None, "signing_bonus": 0, "contract_years": 3},
            "valuation": {"market_value": 5_000_000},
            "contract_round": 0,
            "pressure": 0,
        }),
    }
    cog.get_offer = AsyncMock(return_value=offer)
    cog.market.get_player = AsyncMock(return_value={
        "api_id": 12346, "name": "Roleless Player", "overall": 75, "potential": 78, "age": 24, "position": "CM",
    })
    cog._finalize_agreed_offer = AsyncMock(return_value={"ok": True, "message": "done"})

    interaction = _make_interaction(user_id=42)
    modal = _make_modal(cog, 39, "100k/wk, 2 years")
    await cog.handle_contract_modal_submit(interaction, modal)
    assert interaction.followup.send.await_count >= 1


@pytest.mark.asyncio
async def test_contract_modal_submit_accepts_and_finalizes_with_agreed_terms():
    from bot.cogs.transfers import TransfersCog

    bot = _make_bot()
    bot.llm_service.negotiate_contract = AsyncMock(return_value={
        "accepted": True,
        "walked_away": False,
        "pressure": 1,
        "pressure_bar": "[Negotiation Pressure: █░░░░░░░░░ 10%]",
        "counter": None,
        "agreed_terms": {"wage": 110000, "role": "Important", "signing_bonus": 250000, "contract_years": 3},
        "message": "[Negotiation Pressure: █░░░░░░░░░ 10%]\nDeal.",
    })
    cog = TransfersCog(bot)
    offer = {
        "id": 40,
        "season_id": "S1",
        "guild_id": 1,
        "channel_id": 10,
        "kind": "contract_nego",
        "status": "negotiating",
        "buying_user_id": 42,
        "buying_team_api_id": 10,
        "buying_team_name": "Buying FC",
        "selling_team_api_id": 20,
        "selling_team_name": "Selling FC",
        "selling_manager_user_id": None,
        "player_api_id": 12347,
        "player_name": "Deal Player",
        "fee": 5_000_000,
        "stage": "contract",
        "meta_json": json.dumps({
            "demand": {"wage": 120000, "role": "Important", "signing_bonus": 500000, "contract_years": 3},
            "valuation": {"market_value": 15_000_000},
            "contract_round": 1,
            "pressure": 1,
        }),
    }
    cog.get_offer = AsyncMock(return_value=offer)
    cog.market.get_player = AsyncMock(return_value={
        "api_id": 12347, "name": "Deal Player", "overall": 82, "potential": 84, "age": 26, "position": "RW",
    })
    cog._finalize_agreed_offer = AsyncMock(return_value={"ok": True, "message": "Transfer finalized"})

    interaction = _make_interaction(user_id=42)
    modal = _make_modal(cog, 40, "ok deal")
    await cog.handle_contract_modal_submit(interaction, modal)

    cog._finalize_agreed_offer.assert_awaited_once()
    args = cog._finalize_agreed_offer.await_args.args
    agreed = args[1]
    assert agreed["wage"] == 110000
    assert agreed["role"] == "Important"
    assert agreed["signing_bonus"] == 250000
    assert agreed["contract_years"] == 3
