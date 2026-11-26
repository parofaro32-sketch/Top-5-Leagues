import pytest

from services.nlu_service import parse_money, extract_contract_terms, extract_money_spans
from services.transfer_market_service import money_to_int, fmt_money


class TestParseMoneyRequiredExamples:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("100k", 100_000),
            ("50k", 50_000),
            ("295k", 295_000),
            ("500k", 500_000),
            ("1m", 1_000_000),
            ("1.5m", 1_500_000),
            ("2 million", 2_000_000),
            ("750 thousand", 750_000),
            ("€100k", 100_000),
            ("£1.5m", 1_500_000),
            ("100,000", 100_000),
            ("1,000,000", 1_000_000),
        ],
    )
    def test_parse_money_required_examples(self, raw, expected):
        assert parse_money(raw) == expected
        assert money_to_int(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("100k", 100_000),
            ("50k", 50_000),
            ("295k", 295_000),
            ("500k", 500_000),
            ("1m", 1_000_000),
            ("1.5m", 1_500_000),
            ("2 million", 2_000_000),
            ("750 thousand", 750_000),
            ("€100k", 100_000),
            ("£1.5m", 1_500_000),
            ("100,000", 100_000),
            ("1,000,000", 1_000_000),
        ],
    )
    def test_extract_contract_terms_signing_bonus(self, raw, expected):
        terms = extract_contract_terms(f"{raw} signing bonus, 200k/wk, 3 years, important role")
        assert terms.get("signing_bonus") == expected
        assert terms.get("wage") == 200_000
        assert terms.get("contract_years") == 3
        assert terms.get("role") == "Important"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("100k", 100_000),
            ("50k", 50_000),
            ("295k", 295_000),
            ("500k", 500_000),
            ("1m", 1_000_000),
            ("1.5m", 1_500_000),
            ("2 million", 2_000_000),
            ("750 thousand", 750_000),
            ("€100k", 100_000),
            ("£1.5m", 1_500_000),
            ("100,000", 100_000),
            ("1,000,000", 1_000_000),
        ],
    )
    def test_extract_contract_terms_wage(self, raw, expected):
        terms = extract_contract_terms(f"{raw} per week, 1m signing bonus, 4 years, crucial role")
        assert terms.get("wage") == expected
        assert terms.get("signing_bonus") == 1_000_000
        assert terms.get("contract_years") == 4
        assert terms.get("role") == "Crucial"


class TestMoneyParsingRegression:
    def test_100k_never_becomes_3(self):
        # The reported bug: 100k signing bonus interpreted as $3.
        msg = "100k signing bonus, 200k/wk, 3 years, important role"
        terms = extract_contract_terms(msg)
        assert terms.get("signing_bonus") == 100_000
        assert terms.get("signing_bonus") != 3
        assert terms.get("wage") == 200_000
        assert terms.get("contract_years") == 3

    def test_year_number_does_not_poison_bonus(self):
        # Year number appearing as plain "3" should not be captured as bonus.
        msg = "contract 3, 100k signing bonus, 200k/wk"
        terms = extract_contract_terms(msg)
        assert terms.get("signing_bonus") == 100_000
        assert terms.get("wage") == 200_000
        assert terms.get("contract_years") == 3

    def test_shared_context_window_does_not_misclassify(self):
        msg = "200k/wk, 100k signing bonus, 3 years"
        terms = extract_contract_terms(msg)
        assert terms.get("wage") == 200_000
        assert terms.get("signing_bonus") == 100_000
        assert terms.get("contract_years") == 3

    def test_plain_100k_without_cues_is_not_lost(self):
        assert parse_money("100k") == 100_000

    def test_no_bonus(self):
        terms = extract_contract_terms("200k/wk, no signing bonus, 3 years, important")
        assert terms.get("wage") == 200_000
        assert terms.get("signing_bonus") == 0
        assert terms.get("contract_years") == 3

    def test_signing_bonus_and_wage_separate(self):
        terms = extract_contract_terms("I want 300k a week, crucial role, 2m signing bonus, 4 years")
        assert terms.get("wage") == 300_000
        assert terms.get("signing_bonus") == 2_000_000
        assert terms.get("contract_years") == 4
        assert terms.get("role") == "Crucial"

    def test_currency_and_commas(self):
        assert parse_money("€100,000") == 100_000
        assert parse_money("£1,000,000") == 1_000_000
        assert parse_money("$500k") == 500_000

    def test_money_spans_do_not_capture_year_as_bonus(self):
        msg = "4 year contract, 500k signing bonus, 150k/wk"
        terms = extract_contract_terms(msg)
        assert terms.get("contract_years") == 4
        assert terms.get("signing_bonus") == 500_000
        assert terms.get("wage") == 150_000
        assert terms.get("signing_bonus") != 4


class TestContractFlowSimulation:
    """Simulate the contract modal parsing flow: parse -> offer dict -> evaluate -> storage shape."""

    def _simulate_modal_parse(self, raw_msg: str, demand: dict | None = None):
        """Mirror the core logic from bot/cogs/transfers.py handle_contract_modal_submit."""
        from services.nlu_service import extract_contract_terms
        from services.transfer_market_service import money_to_int, normalize_role
        import re

        demand = demand or {"wage": 100_000, "role": "Important", "signing_bonus": 50_000, "contract_years": 3}
        parsed = extract_contract_terms(raw_msg)

        wage = parsed.get("wage")
        bonus = parsed.get("signing_bonus")
        role = parsed.get("role")
        years = parsed.get("contract_years")

        _bonus_mentioned = bool(re.search(r"\b(signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|sign[- ]?on|upfront|bonus)\b", raw_msg, re.I))
        if bonus is None:
            if _bonus_mentioned and not re.search(r"\b(no|without|zero|none)\b", raw_msg, re.I):
                for pat in (
                    r"(?:signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|sign[- ]?on|upfront|bonus)\s*[:=]?\s*([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|billion|bn)?)",
                    r"([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|billion|bn)?)\s*(?:signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|sign[- ]?on|upfront|bonus)\b",
                ):
                    _m = re.search(pat, raw_msg, re.I)
                    if _m:
                        _v = money_to_int(_m.group(1))
                        if _v is not None:
                            bonus = int(_v)
                            break
            else:
                bonus = 0

        if wage is None or int(wage) <= 0:
            _nums = []
            for _m in re.finditer(r"([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|billion|bn)?)", raw_msg, re.I):
                _v = money_to_int(_m.group(1))
                if _v is not None and _v > 0:
                    _nums.append(int(_v))
            _nums = [v for v in _nums if not (years is not None and int(v) == int(years) and v <= 10)]
            if bonus is not None:
                _nums = [v for v in _nums if int(v) != int(bonus)]
            wage_cands = [v for v in _nums if 1_000 <= v <= 5_000_000]
            if wage_cands:
                wage = int(min(wage_cands))
            elif _nums:
                wage = int(_nums[0])

        if wage is None or int(wage) <= 0:
            wage = int(demand.get("wage") or 50_000)
        if role is None:
            role = normalize_role(str(demand.get("role") or "Rotation")) or "Rotation"
        if bonus is None:
            bonus = int(demand.get("signing_bonus") or 0)
        if years is None:
            years = int(demand.get("contract_years") or 3)

        wage = int(wage)
        bonus = max(0, int(bonus))
        role = normalize_role(str(role)) or str(demand.get("role") or "Rotation")
        years = max(1, min(5, int(years)))

        return {"wage": wage, "role": role, "signing_bonus": bonus, "contract_years": years}

    @pytest.mark.parametrize(
        "raw,expected_bonus",
        [
            ("100k signing bonus, 200k/wk, 3 years, important", 100_000),
            ("50k signing bonus, 120k/wk, 3 years, important", 50_000),
            ("295k signing bonus, 200k/wk, 3 years, important", 295_000),
            ("500k signing bonus, 200k/wk, 3 years, important", 500_000),
            ("1m signing bonus, 200k/wk, 3 years, important", 1_000_000),
            ("1.5m signing bonus, 200k/wk, 3 years, important", 1_500_000),
            ("2 million signing bonus, 200k/wk, 3 years, important", 2_000_000),
            ("750 thousand signing bonus, 200k/wk, 3 years, important", 750_000),
            ("€100k signing bonus, 200k/wk, 3 years, important", 100_000),
            ("£1.5m signing bonus, 200k/wk, 3 years, important", 1_500_000),
            ("100,000 signing bonus, 200k/wk, 3 years, important", 100_000),
            ("1,000,000 signing bonus, 200k/wk, 3 years, important", 1_000_000),
        ],
    )
    def test_full_modal_parse_required_examples(self, raw, expected_bonus):
        offered = self._simulate_modal_parse(raw)
        assert offered["signing_bonus"] == expected_bonus
        assert offered["wage"] == 200_000
        assert offered["contract_years"] == 3
        assert offered["role"] == "Important"

    def test_storage_shape(self):
        offered = self._simulate_modal_parse("100k signing bonus, 200k/wk, 3 years, important")
        # Simulate season_service.create_pending_transfer args
        transfer = {
            "fee": 0,
            "wage": int(offered["wage"]),
            "signing_bonus": int(offered["signing_bonus"]),
            "contract_years": int(offered["contract_years"]),
            "role": offered["role"],
        }
        assert transfer["signing_bonus"] == 100_000
        assert transfer["wage"] == 200_000
        assert transfer["contract_years"] == 3
        assert transfer["role"] == "Important"

        # Simulate confirm_transfer budget deduction
        fee = int(transfer["fee"])
        signing_bonus = int(transfer["signing_bonus"])
        wage = int(transfer["wage"])
        total_transfer_cost = max(0, fee) + max(0, signing_bonus)
        assert total_transfer_cost == 100_000
        assert wage == 200_000

        # Display
        assert fmt_money(signing_bonus) == "€100K"
        assert fmt_money(wage) == "€200K"

    def test_full_flow_100k_never_becomes_3(self):
        offered = self._simulate_modal_parse("contract 3, 100k signing bonus, 200k/wk, important")
        assert offered["signing_bonus"] == 100_000
        assert offered["signing_bonus"] != 3
        assert offered["contract_years"] == 3

        total_transfer_cost = 0 + offered["signing_bonus"]
        assert total_transfer_cost == 100_000
        assert fmt_money(offered["signing_bonus"]) == "€100K"
