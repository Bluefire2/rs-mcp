"""Game formulas: the XP table and combat level."""

import math
from collections.abc import Mapping

MAX_LEVEL = 99
MAX_XP = 200_000_000


def _xp_table() -> list[int]:
    """XP_FOR_LEVEL[n] is the total XP needed for level n (index 0 unused), up to virtual 126."""
    table = [0, 0]
    points = 0
    for level in range(1, 126):
        points += math.floor(level + 300 * 2 ** (level / 7))
        table.append(points // 4)
    return table


XP_FOR_LEVEL = _xp_table()


def xp_to_level(xp: int, target: int) -> int:
    return max(XP_FOR_LEVEL[target] - xp, 0)


def combat_level(levels: Mapping[str, int]) -> int:
    """Standard OSRS combat level from skill levels keyed by capitalised skill name."""
    base = 0.25 * (levels["Defence"] + levels["Hitpoints"] + levels["Prayer"] // 2)
    melee = 0.325 * (levels["Attack"] + levels["Strength"])
    ranged = 0.325 * (levels["Ranged"] * 3 // 2)
    magic = 0.325 * (levels["Magic"] * 3 // 2)
    return math.floor(base + max(melee, ranged, magic))


COMBAT_SKILLS = ("Attack", "Strength", "Defence", "Hitpoints", "Ranged", "Prayer", "Magic")


def normalize_rsn(name: str) -> str:
    """The comparison form of a RuneScape name.

    The game treats spaces and underscores in names as the same character, and RuneLite reports
    the local player's name with non-breaking spaces. Case never matters.
    """
    cleaned = name.replace("\u00a0", " ").replace("_", " ")
    return " ".join(cleaned.split()).lower()
