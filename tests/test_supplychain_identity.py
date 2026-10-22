from quantlab_supplychain.io import listing_code, stock_code


def test_ah_codes_and_ambiguous_mainland_codes_are_not_conflated():
    assert listing_code("601288;01288") == "601288"
    assert listing_code("000001;00388") == "000001"
    assert listing_code("01963") == ""
    assert listing_code("600000;600001") == ""
    assert stock_code("601288;01288") == ""
    assert stock_code("000001") == "000001"
