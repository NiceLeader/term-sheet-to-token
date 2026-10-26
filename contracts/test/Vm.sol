// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

/// @notice The few Foundry cheatcodes the tests use.
interface Vm {
    function warp(uint256 timestamp) external;
    function prank(address sender) external;
    function startPrank(address sender) external;
    function stopPrank() external;
    function expectRevert(bytes4 selector) external;
    function expectRevert(bytes calldata revertData) external;
    function toString(uint256 value) external pure returns (string memory);
}

abstract contract TestBase {
    Vm internal constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function assertEq(uint256 actual, uint256 expected, string memory what) internal pure {
        if (actual != expected) {
            revert(string.concat(what, ": ", vm.toString(actual), " != ", vm.toString(expected)));
        }
    }

    /// @dev 00:00 UTC of a civil date, after H. Hinnant's days_from_civil.
    function day(uint256 y, uint256 m, uint256 d) internal pure returns (uint64) {
        if (m <= 2) y -= 1;
        uint256 era = y / 400;
        uint256 yoe = y - era * 400;
        uint256 mp = m > 2 ? m - 3 : m + 9;
        uint256 doy = (153 * mp + 2) / 5 + d - 1;
        uint256 doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
        return uint64((era * 146097 + doe - 719468) * 1 days);
    }
}
