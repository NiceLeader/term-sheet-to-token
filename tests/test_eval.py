from tstt.eval import normalise


def test_centres_compare_by_calendar():
    assert normalise("business_day", ["London", "T2"]) == normalise("business_day",
                                                                    ["TARGET", "London"])
    assert normalise("business_day", ["New York City"]) == normalise("business_day",
                                                                     ["New York"])
    assert normalise("business_day", ["London"]) != normalise("business_day", ["TARGET"])


def test_empty_values_are_equal():
    assert normalise("record_date", None) == normalise("record_date", "") is None
    assert normalise("stated_coupon_amounts", []) is None
