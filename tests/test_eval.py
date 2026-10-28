from tstt.eval import blocking_fields, normalise


def test_centres_compare_by_calendar():
    assert normalise("business_day", ["London", "T2"]) == normalise("business_day",
                                                                    ["TARGET", "London"])
    assert normalise("business_day", ["New York City"]) == normalise("business_day",
                                                                     ["New York"])
    assert normalise("business_day", ["Frankfurt am Main"]) == normalise("business_day",
                                                                         ["Frankfurt"])
    assert normalise("business_day", ["London"]) != normalise("business_day", ["TARGET"])


def test_empty_values_are_equal():
    assert normalise("record_date", None) == normalise("record_date", "") is None
    assert normalise("stated_coupon_amounts", []) is None


def test_coupon_amounts_compare_as_numbers():
    stated = [{"amount": "9000000", "per": "3000000000"}]
    written = [{"amount": "9000000.00", "per": "3,000,000,000"}]
    assert normalise("stated_coupon_amounts", stated) == normalise("stated_coupon_amounts",
                                                                   written)


def test_a_missing_schedule_term_or_unreadable_value_blocks_the_token():
    assert blocking_fields({"final_redemption_amount": None, "interest_rate": "three",
                            "record_date": None}) >= {"final_redemption_amount", "interest_rate"}
    assert "record_date" not in blocking_fields({"record_date": None})


def test_interest_on_holding_not_stated_means_per_note():
    assert normalise("interest_on_holding", None) == normalise("interest_on_holding", False)
    assert normalise("interest_on_holding", True) != normalise("interest_on_holding", None)
