// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

interface ICash {
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

/// @title Standard fixed rate bond token
/// @notice One unit is one note of the smallest specified denomination. The terms are set once
/// at deployment. Every coupon is computed on chain from the accrual segments, the day count
/// fraction and the rounding rule, then paid in a settlement token to the holders of record.
/// @dev Payments push to every holder, which keeps the lifecycle easy to audit for a small
/// holder base. A production deployment would switch to claims for large holder sets.
contract BondToken {
    uint256 public constant RATE_SCALE = 1e8; // percent per annum, 3.399% = 339_900_000
    uint256 public constant PERCENT_SCALE = 1e8; // percent of nominal, 100% = 10_000_000_000
    uint256 private constant DAY = 1 days;

    enum DayCount {
        ActActICMA,
        Thirty360,
        ThirtyE360,
        Act365Fixed,
        Act360,
        RbaBondBasis,
        ActActCanadian
    }

    enum Rounding {
        HalfUp,
        HalfEven,
        Down,
        Up
    }

    struct Terms {
        string isin;
        string currency;
        address cash;
        address issuer;
        address payingAgent;
        uint256 totalUnits;
        uint256 denomination; // cash minor units per note
        uint256 calculationAmount; // cash minor units on which a coupon is computed and rounded
        uint256 rate; // percent per annum times RATE_SCALE
        uint8 frequency;
        DayCount dayCount;
        Rounding rounding;
        uint256 redemptionPercent; // percent of nominal times PERCENT_SCALE
        uint64 maturityPaymentDate;
        uint64 redemptionRecordDate;
    }

    /// @notice Part of an accrual period that lies inside one regular (notional) period.
    struct Segment {
        uint64 start;
        uint64 end;
        uint64 refStart;
        uint64 refEnd;
    }

    /// @dev All dates are 00:00 UTC of the calendar day. Holders of record are those at the
    /// close of the record date, so a transfer on the record date itself still counts.
    struct Payment {
        uint64 date;
        uint64 recordDate;
        uint16 firstSegment;
        uint16 segmentCount;
        bool paid;
    }

    struct Checkpoint {
        uint64 time;
        uint192 units;
    }

    Terms internal terms;
    Segment[] public segments;
    Payment[] public payments;
    address[] public holders;
    bool public issued;
    bool public redeemed;

    mapping(address => Checkpoint[]) private _checkpoints;
    mapping(address => bool) private _known;

    event TermsConfigured(string isin, uint256 payments, uint256 segments);
    event Transfer(address indexed from, address indexed to, uint256 units);
    event CouponPaid(uint256 indexed payment, address indexed holder, uint256 units, uint256 amount);
    event PaymentExecuted(
        uint256 indexed payment, uint64 scheduledDate, uint256 perCalculationAmount, uint256 total
    );
    event Redeemed(address indexed holder, uint256 units, uint256 amount);
    event RedemptionExecuted(uint64 scheduledDate, uint256 perUnit, uint256 total);

    error NotIssuer();
    error NotPayingAgent();
    error InvalidTerms(string reason);
    error AlreadyIssued();
    error TooManyUnits();
    error TooEarly();
    error AlreadyPaid();
    error CouponsOutstanding();
    error BondRedeemed();
    error InsufficientUnits();

    constructor(Terms memory terms_, Segment[] memory segments_, Payment[] memory payments_) {
        if (terms_.calculationAmount == 0 || terms_.denomination % terms_.calculationAmount != 0) {
            revert InvalidTerms("denomination must be a multiple of the calculation amount");
        }
        if (12 % uint256(terms_.frequency) != 0) revert InvalidTerms("frequency");
        if (payments_.length == 0) revert InvalidTerms("no payments");
        terms = terms_;
        for (uint256 i = 0; i < segments_.length; i++) {
            Segment memory s = segments_[i];
            if (s.end <= s.start || s.refEnd <= s.refStart) revert InvalidTerms("segment order");
            segments.push(s);
        }
        for (uint256 i = 0; i < payments_.length; i++) {
            Payment memory p = payments_[i];
            if (uint256(p.firstSegment) + p.segmentCount > segments_.length || p.segmentCount == 0) {
                revert InvalidTerms("payment segments");
            }
            if (p.recordDate > p.date) revert InvalidTerms("record date after payment date");
            p.paid = false;
            payments.push(p);
        }
        emit TermsConfigured(terms_.isin, payments_.length, segments_.length);
    }

    // ---------------------------------------------------------------- holdings

    function issue(address[] calldata to, uint256[] calldata units) external {
        if (msg.sender != terms.issuer) revert NotIssuer();
        if (issued) revert AlreadyIssued();
        uint256 total;
        for (uint256 i = 0; i < to.length; i++) {
            total += units[i];
            _credit(to[i], units[i]);
            emit Transfer(address(0), to[i], units[i]);
        }
        if (total > terms.totalUnits) revert TooManyUnits();
        issued = true;
    }

    function transfer(address to, uint256 units) external returns (bool) {
        if (redeemed) revert BondRedeemed();
        uint256 balance = balanceOf(msg.sender);
        if (balance < units) revert InsufficientUnits();
        _write(msg.sender, balance - units);
        _credit(to, units);
        emit Transfer(msg.sender, to, units);
        return true;
    }

    function balanceOf(address holder) public view returns (uint256) {
        Checkpoint[] storage cps = _checkpoints[holder];
        return cps.length == 0 ? 0 : cps[cps.length - 1].units;
    }

    /// @notice Units held at the end of second `time`.
    function balanceAt(address holder, uint64 time) public view returns (uint256) {
        Checkpoint[] storage cps = _checkpoints[holder];
        for (uint256 i = cps.length; i > 0; i--) {
            if (cps[i - 1].time <= time) return cps[i - 1].units;
        }
        return 0;
    }

    /// @notice Units held at the close of the record date `day` (00:00 UTC timestamp).
    function balanceOfRecord(address holder, uint64 day) public view returns (uint256) {
        return balanceAt(holder, day + uint64(DAY) - 1);
    }

    function getTerms() external view returns (Terms memory) {
        return terms;
    }

    function holderCount() external view returns (uint256) {
        return holders.length;
    }

    function paymentCount() external view returns (uint256) {
        return payments.length;
    }

    function _credit(address holder, uint256 units) private {
        if (!_known[holder]) {
            _known[holder] = true;
            holders.push(holder);
        }
        _write(holder, balanceOf(holder) + units);
    }

    function _write(address holder, uint256 units) private {
        Checkpoint[] storage cps = _checkpoints[holder];
        uint64 now_ = uint64(block.timestamp);
        if (cps.length > 0 && cps[cps.length - 1].time == now_) {
            cps[cps.length - 1].units = uint192(units);
        } else {
            cps.push(Checkpoint(now_, uint192(units)));
        }
    }

    // ---------------------------------------------------------------- interest

    /// @notice Coupon for payment `i` per calculation amount, in cash minor units.
    function couponPerCalculationAmount(uint256 i) public view returns (uint256) {
        Payment storage p = payments[i];
        uint256 num = 0;
        uint256 den = 1;
        for (uint256 k = p.firstSegment; k < uint256(p.firstSegment) + p.segmentCount; k++) {
            (uint256 n, uint256 d) = accrualFraction(segments[k]);
            num = num * d + n * den;
            den = den * d;
        }
        return _round(terms.calculationAmount * terms.rate * num, 100 * RATE_SCALE * den);
    }

    function couponPerUnit(uint256 i) public view returns (uint256) {
        return couponPerCalculationAmount(i) * (terms.denomination / terms.calculationAmount);
    }

    function redemptionPerUnit() public view returns (uint256) {
        return terms.denomination * terms.redemptionPercent / (100 * PERCENT_SCALE);
    }

    /// @notice Day count fraction of one segment as numerator and denominator.
    function accrualFraction(Segment memory s) public view returns (uint256, uint256) {
        uint256 actualDays = (s.end - s.start) / DAY;
        DayCount dc = terms.dayCount;
        if (dc == DayCount.ActActICMA) {
            return (actualDays, uint256(terms.frequency) * ((s.refEnd - s.refStart) / DAY));
        }
        if (dc == DayCount.Thirty360) return (_days360(s.start, s.end, false), 360);
        if (dc == DayCount.ThirtyE360) return (_days360(s.start, s.end, true), 360);
        if (dc == DayCount.Act365Fixed) return (actualDays, 365);
        if (dc == DayCount.Act360) return (actualDays, 360);
        // RBA Bond Basis and Actual/Actual Canadian Compound: a regular period pays rate over
        // frequency, an irregular one accrues actual days over 365.
        if (s.start == s.refStart && s.end == s.refEnd) return (1, uint256(terms.frequency));
        return (actualDays, 365);
    }

    function _days360(uint64 start, uint64 end, bool european) private pure returns (uint256) {
        (uint256 y1, uint256 m1, uint256 d1) = _civil(start);
        (uint256 y2, uint256 m2, uint256 d2) = _civil(end);
        if (d1 == 31) d1 = 30;
        if (european) {
            if (d2 == 31) d2 = 30;
        } else if (d2 == 31 && d1 == 30) {
            d2 = 30;
        }
        return 360 * y2 + 30 * m2 + d2 - (360 * y1 + 30 * m1 + d1);
    }

    /// @dev Civil date from a unix timestamp (proleptic Gregorian), after H. Hinnant.
    function _civil(uint64 timestamp) private pure returns (uint256 y, uint256 m, uint256 d) {
        uint256 z = uint256(timestamp) / DAY + 719468;
        uint256 era = z / 146097;
        uint256 doe = z - era * 146097;
        uint256 yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
        uint256 doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        uint256 mp = (5 * doy + 2) / 153;
        d = doy - (153 * mp + 2) / 5 + 1;
        m = mp < 10 ? mp + 3 : mp - 9;
        y = yoe + era * 400 + (m <= 2 ? 1 : 0);
    }

    function _round(uint256 num, uint256 den) private view returns (uint256) {
        uint256 q = num / den;
        uint256 r = num % den;
        Rounding rule = terms.rounding;
        if (rule == Rounding.HalfUp) {
            if (2 * r >= den) q += 1;
        } else if (rule == Rounding.HalfEven) {
            if (2 * r > den || (2 * r == den && q % 2 == 1)) q += 1;
        } else if (rule == Rounding.Up) {
            if (r > 0) q += 1;
        }
        return q;
    }

    // ---------------------------------------------------------------- payments

    function payCoupon(uint256 i) external {
        if (msg.sender != terms.payingAgent) revert NotPayingAgent();
        Payment storage p = payments[i];
        if (p.paid) revert AlreadyPaid();
        if (block.timestamp < p.date) revert TooEarly();
        p.paid = true;
        uint256 perCalc = couponPerCalculationAmount(i);
        uint256 perUnit = perCalc * (terms.denomination / terms.calculationAmount);
        uint256 total;
        for (uint256 h = 0; h < holders.length; h++) {
            address holder = holders[h];
            uint256 units = balanceOfRecord(holder, p.recordDate);
            if (units == 0) continue;
            uint256 amount = units * perUnit;
            total += amount;
            ICash(terms.cash).transferFrom(terms.payingAgent, holder, amount);
            emit CouponPaid(i, holder, units, amount);
        }
        emit PaymentExecuted(i, p.date, perCalc, total);
    }

    function redeem() external {
        if (msg.sender != terms.payingAgent) revert NotPayingAgent();
        if (redeemed) revert BondRedeemed();
        if (block.timestamp < terms.maturityPaymentDate) revert TooEarly();
        for (uint256 i = 0; i < payments.length; i++) {
            if (!payments[i].paid) revert CouponsOutstanding();
        }
        redeemed = true;
        uint256 perUnit = redemptionPerUnit();
        uint256 total;
        for (uint256 h = 0; h < holders.length; h++) {
            address holder = holders[h];
            uint256 units = balanceOfRecord(holder, terms.redemptionRecordDate);
            if (units == 0) continue;
            uint256 amount = units * perUnit;
            total += amount;
            _write(holder, 0);
            ICash(terms.cash).transferFrom(terms.payingAgent, holder, amount);
            emit Redeemed(holder, units, amount);
        }
        emit RedemptionExecuted(terms.maturityPaymentDate, perUnit, total);
    }
}
