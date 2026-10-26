# Ground truth

One JSON file per bond in `groundtruth/<bond_id>.json`, labelled by hand from the public
documents listed in `sources/manifest.json`. A file stays `"status": "draft"` until every field
has been checked against the page it cites. The review screen (`tstt serve`, source "Verified
terms") records that check field by field, writes any correction into the file, and sets
`"status": "verified"` once every field is reviewed and the bond replays without a red flag.

## Format

```json
{
  "bond_id": "ibrd-eur-3399-2026",
  "isin": "XS2615318289",
  "status": "draft",
  "fields": {
    "interest_rate": {"value": "3.399", "doc": "ibrd-xs2615318289-ft", "page": 2},
    "day_count_fraction": {
      "value": "ACT_ACT_ICMA",
      "doc": "ibrd-xs2615318289-ft", "page": 2,
      "definition": {"doc": "ibrd-dnn-supplement-2023", "page": 21}
    }
  },
  "notes": "Anything a reviewer should know, e.g. which supplement overrides the base conditions."
}
```

- `doc` and `page` point to where the term is stated. Pages are 1-based physical pages of the
  PDF, or the page numbers produced by `tstt parse` for EDGAR HTML.
- `definition` points to where the meaning of the stated term is defined, usually in the base
  prospectus or a supplement. It is required for `day_count_fraction`,
  `business_day_convention`, `business_day` and `rounding_rule` and optional elsewhere.
- A field may carry `"check": "<reason>"` to send the reviewer to an ambiguous value first.
- A term the documents do not state has `"value": null` and no page.

## Fields and value formats

| Field | Value format |
|---|---|
| `isin` | ISIN, or `null` if the documents give only a CUSIP |
| `issuer` | Issuer name as written |
| `currency` | ISO 4217 code of the specified currency |
| `aggregate_nominal_amount` | Decimal, no separators, e.g. `"100000000"` (this tranche, not later taps) |
| `specified_denomination` | Smallest specified denomination, decimal |
| `calculation_amount` | Calculation Amount if stated, else `null` |
| `issue_date` | `YYYY-MM-DD` |
| `interest_commencement_date` | `YYYY-MM-DD`; equal to the issue date if the documents say so or say nothing |
| `maturity_date` | `YYYY-MM-DD`, unadjusted |
| `interest_rate` | Percent per annum as decimal, e.g. `"3.399"` |
| `coupon_frequency` | Payments per year: `1`, `2`, `4` or `12` |
| `interest_payment_dates` | Month-day list, e.g. `["04-10", "10-10"]` |
| `first_interest_payment_date` | `YYYY-MM-DD` |
| `day_count_fraction` | `ACT_ACT_ICMA`, `THIRTY_360`, `THIRTY_E_360`, `ACT_365_FIXED`, `ACT_360`, `RBA_BOND_BASIS`, `ACT_ACT_CANADIAN_COMPOUND`, `OTHER` |
| `business_day_convention` | Convention for payment dates: `FOLLOWING`, `MODIFIED_FOLLOWING`, `PRECEDING`, `NONE` |
| `accrual_adjusted` | `true` if interest periods move with the convention, `false` if unadjusted |
| `business_day` | Financial centres whose business days count, e.g. `["London", "TARGET", "New York"]` |
| `final_redemption_amount` | Percent of nominal, e.g. `"100"` |
| `rounding_rule` | Rounding of coupon amounts to the sub-unit: `HALF_UP`, `HALF_EVEN`, `DOWN`, `UP`, `NOT_STATED` |
| `stated_coupon_amounts` | Coupon amounts printed in the documents, list of `{"date": "YYYY-MM-DD" or null, "amount": "33.99", "per": "1000"}`; a fixed coupon amount that applies to every regular date has `date: null` |
| `record_date` | Record date rule as a short phrase, or `null` |
