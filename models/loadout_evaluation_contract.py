"""Phase D evaluation and Phase E promotion contracts for Loadout Availability."""

PHASE_D_SPLITS = (
    "rolling_chronological",
    "designated_player_holdout",
    "balanced_multi_player_leaveout",
    "completely_unseen_players",
)

PROMOTION_GATES = {
    "unseen_player_auc": (">=", 0.65),
    "brier_improvement_over_base": (">=", 0.05),
    "ece": ("<=", 0.05),
    "shadow_days": (">=", 14),
}


def phase_d_stages() -> tuple[int, ...]:
    return (0, 1, 2, 3, 4)


def evaluation_gate_passes(metrics: dict) -> bool:
    return (
        metrics.get("unseen_player_auc", 0) >= 0.65
        and metrics.get("brier_improvement_over_base", 0) >= 0.05
        and metrics.get("ece", 1) <= 0.05
        and not metrics.get("major_subgroup_collapse", True)
    )


def promotion_gate_passes(*, evaluation_eligible: bool, shadow_days: float,
                          shadow_healthy: bool, explicit_approval: bool) -> bool:
    return evaluation_eligible and shadow_days >= 14 and shadow_healthy and explicit_approval
