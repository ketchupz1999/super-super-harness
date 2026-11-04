"""Permanent Worker code names, shared by the Router and every web surface."""
import re

WORKER_NAMES = (
    "Nova", "Pixel", "Echo", "Atlas", "Juno", "Orbit", "Sage", "Milo",
    "Iris", "Bolt", "Luna", "Rex", "Kiko", "Zed", "Ivy", "Taro",
    "Mochi", "Finn", "Nori", "Pip", "Remy", "Skye", "Toto", "Wren",
    "Amber", "Aspen", "Aster", "Aurora", "Basil", "Birch", "Brook", "Cedar",
    "Clover", "Comet", "Coral", "Cosmo", "Dawn", "Delta", "Dune", "Ember",
    "Fern", "Flint", "Flora", "Frost", "Gale", "Hazel", "Indigo", "Jade",
    "Jasper", "Jet", "Kai", "Kestrel", "Lark", "Laurel", "Leo", "Lotus",
    "Maple", "Mica", "Mist", "Moss", "Nimbus", "Oak", "Olive", "Onyx",
    "Opal", "Orion", "Pearl", "Pebble", "Pine", "Pluto", "Poppy", "Quartz",
    "Quill", "Rain", "Reed", "River", "Robin", "Rowan", "Ruby", "Rune",
    "Sable", "Scout", "Sequoia", "Sol", "Sora", "Spark", "Spruce", "Star",
    "Sunny", "Terra", "Thyme", "Vale", "Vega", "Willow", "Yarrow", "Zephyr",
)
CODE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,23}")


def check_code_name(name):
    if not isinstance(name, str) or not CODE_NAME.fullmatch(name):
        raise ValueError("code_name must be 1–24 ASCII letters, digits or '-', starting with a letter")
    return name


def unused_name(used):
    """First unused pool name; after the pool, try numbered rounds. Never wrap."""
    suffix = ""
    number = 1
    while True:
        for name in WORKER_NAMES:
            candidate = name[:24 - len(suffix)] + suffix
            if candidate.lower() not in used:
                return candidate
        number += 1
        suffix = str(number)


def named_task(row):
    return dict(row) | {"worker_name": row["code_name"]}
