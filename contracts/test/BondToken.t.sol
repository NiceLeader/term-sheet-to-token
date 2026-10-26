// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

import {BondToken} from "../src/BondToken.sol";
import {CashToken} from "../src/CashToken.sol";
import {TestBase} from "./Vm.sol";

contract BondTokenTest is TestBase {
    address internal constant ISSUER = address(0x1001);
    address internal constant AGENT = address(0x1002);
    address internal constant ALICE = address(0x2001);
    address internal constant BOB = address(0x2002);

    CashToken internal cash;

    function setUp() public {
        cash = new CashToken("Euro deposit token", "EURD", 2);
    }

    // ------------------------------------------------------------ helpers

    function terms(BondToken.DayCount dc, BondToken.Rounding rounding, uint256 rate, uint8 frequency, uint64 maturity)
        internal
        view
        returns (BondToken.Terms memory)
    {
        return BondToken.Terms({
            isin: "XS0000000001",
            currency: "EUR",
            cash: address(cash),
            issuer: ISSUER,
            payingAgent: AGENT,
            totalUnits: 1000,
            denomination: 100_000,
            calculationAmount: 100_000,
            rate: rate,
            frequency: frequency,
            dayCount: dc,
            rounding: rounding,
            redemptionPercent: 100 * 1e8,
            maturityPaymentDate: maturity,
            redemptionRecordDate: maturity - 1 days
        });
    }

    function seg(uint64 start, uint64 end) internal pure returns (BondToken.Segment memory) {
        return BondToken.Segment(start, end, start, end);
    }

    /// @dev One payment per segment, paid on the segment end, record date the day before.
    function onePaymentPerSegment(BondToken.Segment[] memory segments)
        internal
        pure
        returns (BondToken.Payment[] memory payments)
    {
        payments = new BondToken.Payment[](segments.length);
        for (uint256 i = 0; i < segments.length; i++) {
            payments[i] = BondToken.Payment(segments[i].end, segments[i].end - 1 days, uint16(i), 1, false);
        }
    }

    function single(BondToken.Terms memory t, BondToken.Segment memory s) internal returns (BondToken) {
        BondToken.Segment[] memory segments = new BondToken.Segment[](1);
        segments[0] = s;
        return new BondToken(t, segments, onePaymentPerSegment(segments));
    }

    // ------------------------------------------------------------ coupon amounts

    function test_regularAnnualActActIcma() public {
        // IBRD EUR 3.399% 2026: EUR 33.99 per EUR 1,000 each year.
        BondToken.Segment[] memory segments = new BondToken.Segment[](3);
        segments[0] = seg(day(2023, 10, 23), day(2024, 10, 23));
        segments[1] = seg(day(2024, 10, 23), day(2025, 10, 23));
        segments[2] = seg(day(2025, 10, 23), day(2026, 10, 23));
        BondToken bond = new BondToken(
            terms(BondToken.DayCount.ActActICMA, BondToken.Rounding.HalfUp, 339_900_000, 1, day(2026, 10, 23)),
            segments,
            onePaymentPerSegment(segments)
        );
        for (uint256 i = 0; i < 3; i++) {
            assertEq(bond.couponPerCalculationAmount(i), 3399, "annual coupon");
        }
        assertEq(bond.redemptionPerUnit(), 100_000, "redemption");
    }

    function test_shortFirstActActIcmaMatchesPrintedAmount() public {
        // IBRD GBP 0.750% 2026: GBP 6.45 per GBP 1,000 for the short first coupon.
        BondToken bond = single(
            terms(BondToken.DayCount.ActActICMA, BondToken.Rounding.HalfUp, 75_000_000, 1, day(2026, 12, 15)),
            BondToken.Segment(day(2020, 2, 4), day(2020, 12, 15), day(2019, 12, 15), day(2020, 12, 15))
        );
        assertEq(bond.couponPerCalculationAmount(0), 645, "short first coupon");
    }

    function test_longFirstThirty360SumsSegments() public {
        // KfW USD 4.25% 2029: USD 69,062,500 on USD 3bn for the long first coupon.
        BondToken.Terms memory t =
            terms(BondToken.DayCount.Thirty360, BondToken.Rounding.HalfUp, 425_000_000, 2, day(2029, 7, 30));
        t.denomination = 300_000_000_000;
        t.calculationAmount = 300_000_000_000;
        BondToken.Segment[] memory segments = new BondToken.Segment[](2);
        segments[0] = BondToken.Segment(day(2026, 7, 15), day(2026, 7, 30), day(2026, 1, 30), day(2026, 7, 30));
        segments[1] = seg(day(2026, 7, 30), day(2027, 1, 30));
        BondToken.Payment[] memory payments = new BondToken.Payment[](1);
        payments[0] = BondToken.Payment(day(2027, 2, 1), day(2027, 1, 29), 0, 2, false);
        BondToken bond = new BondToken(t, segments, payments);
        assertEq(bond.couponPerCalculationAmount(0), 6_906_250_000, "long first coupon");
    }

    function test_roundingRuleDecidesHalfCent() public {
        // HKSAR USD 3.625% digital notes: 18.125 per 1,000, printed as 18.12.
        BondToken.Segment memory s = seg(day(2025, 11, 12), day(2026, 5, 12));
        uint64 maturity = day(2028, 11, 12);
        BondToken.DayCount dc = BondToken.DayCount.Thirty360;
        assertEq(single(terms(dc, BondToken.Rounding.HalfUp, 362_500_000, 2, maturity), s)
            .couponPerCalculationAmount(0), 1813, "half up");
        assertEq(single(terms(dc, BondToken.Rounding.HalfEven, 362_500_000, 2, maturity), s)
            .couponPerCalculationAmount(0), 1812, "half even");
        assertEq(single(terms(dc, BondToken.Rounding.Down, 362_500_000, 2, maturity), s)
            .couponPerCalculationAmount(0), 1812, "down");
        assertEq(single(terms(dc, BondToken.Rounding.Up, 362_500_000, 2, maturity), s)
            .couponPerCalculationAmount(0), 1813, "up");
    }

    function test_thirty360EndOfMonthRules() public {
        uint64 maturity = day(2030, 1, 1);
        BondToken bond = single(
            terms(BondToken.DayCount.Thirty360, BondToken.Rounding.HalfUp, 1e8, 2, maturity),
            seg(day(2026, 1, 31), day(2026, 7, 31))
        );
        (uint256 n, uint256 d) = bond.accrualFraction(seg(day(2026, 1, 31), day(2026, 7, 31)));
        assertEq(n * 360 / d, 180, "30/360 from the 31st to the 31st");
        (n, d) = bond.accrualFraction(seg(day(2026, 2, 28), day(2026, 8, 31)));
        assertEq(n * 360 / d, 183, "30/360 keeps D2 = 31 when D1 < 30");

        BondToken euro = single(
            terms(BondToken.DayCount.ThirtyE360, BondToken.Rounding.HalfUp, 1e8, 2, maturity),
            seg(day(2026, 1, 31), day(2026, 7, 31))
        );
        (n, d) = euro.accrualFraction(seg(day(2026, 2, 28), day(2026, 8, 31)));
        assertEq(n * 360 / d, 182, "30E/360 cuts D2 = 31");
        (n, d) = euro.accrualFraction(seg(day(2027, 12, 31), day(2028, 2, 29)));
        assertEq(n * 360 / d, 59, "30E/360 across a leap February");
    }

    function test_actualConventions() public {
        uint64 maturity = day(2030, 1, 1);
        BondToken.Segment memory s = seg(day(2027, 12, 31), day(2028, 6, 30)); // 182 days
        BondToken fixed365 =
            single(terms(BondToken.DayCount.Act365Fixed, BondToken.Rounding.HalfUp, 1e8, 2, maturity), s);
        (uint256 n, uint256 d) = fixed365.accrualFraction(s);
        assertEq(n, 182, "actual days");
        assertEq(d, 365, "365 fixed");
        BondToken act360 = single(terms(BondToken.DayCount.Act360, BondToken.Rounding.HalfUp, 1e8, 2, maturity), s);
        (, d) = act360.accrualFraction(s);
        assertEq(d, 360, "actual/360");
        BondToken rba = single(terms(BondToken.DayCount.RbaBondBasis, BondToken.Rounding.HalfUp, 1e8, 2, maturity), s);
        (n, d) = rba.accrualFraction(s);
        assertEq(n * 1000 / d, 500, "RBA regular period is one over frequency");
        (n, d) = rba.accrualFraction(BondToken.Segment(day(2028, 1, 31), day(2028, 6, 30), s.start, s.end));
        assertEq(n, 151, "RBA irregular period counts actual days");
        assertEq(d, 365, "RBA irregular period over 365");
    }

    // ------------------------------------------------------------ lifecycle

    function lifecycleBond() internal returns (BondToken bond) {
        BondToken.Segment[] memory segments = new BondToken.Segment[](2);
        segments[0] = seg(day(2026, 3, 2), day(2027, 3, 2));
        segments[1] = seg(day(2027, 3, 2), day(2028, 3, 2));
        BondToken.Payment[] memory payments = new BondToken.Payment[](2);
        payments[0] = BondToken.Payment(day(2027, 3, 2), day(2027, 2, 15), 0, 1, false);
        payments[1] = BondToken.Payment(day(2028, 3, 2), day(2028, 2, 15), 1, 1, false);
        BondToken.Terms memory t =
            terms(BondToken.DayCount.ActActICMA, BondToken.Rounding.HalfUp, 250_000_000, 1, day(2028, 3, 2));
        t.redemptionRecordDate = day(2028, 2, 15);
        bond = new BondToken(t, segments, payments);

        vm.warp(day(2026, 3, 2));
        address[] memory to = new address[](2);
        uint256[] memory units = new uint256[](2);
        (to[0], units[0], to[1], units[1]) = (ALICE, 600, BOB, 400);
        vm.prank(ISSUER);
        bond.issue(to, units);

        cash.mint(AGENT, 10_000_000_000);
        vm.prank(AGENT);
        cash.approve(address(bond), type(uint256).max);
    }

    function test_couponGoesToHolderOfRecord() public {
        BondToken bond = lifecycleBond();
        // Alice sells 100 notes after the record date: she still receives this coupon.
        vm.warp(day(2027, 2, 20));
        vm.prank(ALICE);
        bond.transfer(BOB, 100);

        vm.warp(day(2027, 3, 2));
        vm.prank(AGENT);
        bond.payCoupon(0);
        assertEq(cash.balanceOf(ALICE), 600 * 2500, "alice coupon on 600 units");
        assertEq(cash.balanceOf(BOB), 400 * 2500, "bob coupon on 400 units");
    }

    function test_transferOnRecordDateCounts() public {
        BondToken bond = lifecycleBond();
        vm.warp(day(2027, 2, 15) + 12 hours);
        vm.prank(ALICE);
        bond.transfer(BOB, 100);
        vm.warp(day(2027, 3, 2));
        vm.prank(AGENT);
        bond.payCoupon(0);
        assertEq(cash.balanceOf(ALICE), 500 * 2500, "alice after record date transfer");
        assertEq(cash.balanceOf(BOB), 500 * 2500, "bob after record date transfer");
    }

    function test_paymentGuards() public {
        BondToken bond = lifecycleBond();
        vm.warp(day(2027, 3, 2) - 1);
        vm.prank(AGENT);
        vm.expectRevert(BondToken.TooEarly.selector);
        bond.payCoupon(0);

        vm.warp(day(2027, 3, 2));
        vm.prank(ALICE);
        vm.expectRevert(BondToken.NotPayingAgent.selector);
        bond.payCoupon(0);

        vm.prank(AGENT);
        bond.payCoupon(0);
        vm.prank(AGENT);
        vm.expectRevert(BondToken.AlreadyPaid.selector);
        bond.payCoupon(0);
    }

    function test_redemptionAfterFinalCoupon() public {
        BondToken bond = lifecycleBond();
        vm.warp(day(2027, 3, 2));
        vm.prank(AGENT);
        bond.payCoupon(0);

        vm.warp(day(2028, 3, 2));
        vm.prank(AGENT);
        vm.expectRevert(BondToken.CouponsOutstanding.selector);
        bond.redeem();

        vm.startPrank(AGENT);
        bond.payCoupon(1);
        bond.redeem();
        vm.stopPrank();
        assertEq(bond.balanceOf(ALICE), 0, "alice units burned");
        assertEq(bond.balanceOf(BOB), 0, "bob units burned");
        assertEq(cash.balanceOf(ALICE), 600 * (2 * 2500 + 100_000), "alice cash");
        assertEq(cash.balanceOf(BOB), 400 * (2 * 2500 + 100_000), "bob cash");

        vm.prank(ALICE);
        vm.expectRevert(BondToken.BondRedeemed.selector);
        bond.transfer(BOB, 1);
    }

    function test_redemptionAbovePar() public {
        BondToken.Terms memory t =
            terms(BondToken.DayCount.ActActICMA, BondToken.Rounding.HalfUp, 250_000_000, 1, day(2027, 3, 2));
        t.redemptionPercent = 101_25 * 1e6; // 101.25 per cent
        BondToken bond = single(t, seg(day(2026, 3, 2), day(2027, 3, 2)));
        assertEq(bond.redemptionPerUnit(), 101_250, "101.25 per cent of 100,000.00");
    }

    function test_rejectsInconsistentTerms() public {
        BondToken.Terms memory t =
            terms(BondToken.DayCount.ActActICMA, BondToken.Rounding.HalfUp, 1e8, 5, day(2030, 1, 1));
        BondToken.Segment[] memory segments = new BondToken.Segment[](1);
        segments[0] = seg(day(2029, 1, 1), day(2030, 1, 1));
        BondToken.Payment[] memory payments = onePaymentPerSegment(segments);
        vm.expectRevert(abi.encodeWithSelector(BondToken.InvalidTerms.selector, "frequency"));
        new BondToken(t, segments, payments);
    }
}
