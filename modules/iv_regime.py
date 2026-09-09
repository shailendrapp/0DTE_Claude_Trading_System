"""VIX-based IV regime classifier -- generalizes Vajra's single VIX-20 split."""
from enum import Enum
import config


class IVRegime(str, Enum):
    LOW = "low"          # VIX < 20 -- Vajra's old Iron Fly zone
    ELEVATED = "elevated"  # 20 <= VIX < 28
    EXTREME = "extreme"    # VIX >= 28 -- tail risk regime, downsize hard


def classify(vix: float) -> IVRegime:
    if vix < config.VIX_LOW_HIGH_BREAK:
        return IVRegime.LOW
    if vix < config.VIX_HIGH_EXTREME_BREAK:
        return IVRegime.ELEVATED
    return IVRegime.EXTREME
