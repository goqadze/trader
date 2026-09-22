from dataclasses import dataclass


@dataclass
class PositionPlan:
    """A fully-sized trade: how many shares, and what you stand to lose or win."""

    shares: int  # how many shares to buy (rounded down so we never risk more than planned)
    risk_amount: float  # dollars lost if the stop-loss is hit
    reward_amount: float  # dollars gained if the target is hit
    risk_reward_ratio: float  # reward / risk — want this >= 2.0


def position_size(
    account_balance: float,
    entry: float,
    stop_loss: float,
    target: float | None = None,
    risk_pct: float = 0.02,
) -> PositionPlan:
    """Decide how many shares to buy by risking a fixed % of the account.

    The idea: pick your dollar risk FIRST, then let the math set the share count.
    That way a losing trade can never cost more than `risk_pct` of the account.

        shares = (account_balance * risk_pct) / (entry - stop_loss)
    """
    # Step 1: the most we're willing to lose on this one trade (e.g. 2% of $500 = $10)
    risk_amount = account_balance * risk_pct

    # Step 2: how much we lose per share if the stop is hit (must be positive for a long trade)
    risk_per_share = entry - stop_loss
    if risk_per_share <= 0:
        raise ValueError("stop_loss must be below entry for a long trade")

    # Step 3: shares = money-at-risk / risk-per-share, rounded DOWN so we stay within budget
    shares = int(risk_amount // risk_per_share)

    # Actual risk after rounding (5 shares * $2 = $10), and reward if a target was given
    actual_risk = shares * risk_per_share
    reward_amount = shares * (target - entry) if target is not None else 0.0
    # Risk/reward ratio: reward divided by risk. 0.0 when there's no target or no risk.
    ratio = (reward_amount / actual_risk) if actual_risk > 0 else 0.0

    return PositionPlan(
        shares=shares,
        risk_amount=round(actual_risk, 2),
        reward_amount=round(reward_amount, 2),
        risk_reward_ratio=round(ratio, 2),
    )
