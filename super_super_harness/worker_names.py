"""Stable Worker identities, shared by the Router and every web surface."""

# Keep this order: existing Workers retain the names assigned by the v2 page.
WORKER_NAMES = (
    "Nova", "Pixel", "Echo", "Atlas", "Juno", "Orbit", "Sage", "Milo",
    "Iris", "Bolt", "Luna", "Rex", "Kiko", "Zed", "Ivy", "Taro",
    "Mochi", "Finn", "Nori", "Pip", "Remy", "Skye", "Toto", "Wren",
)


def worker_name(seq):
    return WORKER_NAMES[(seq - 1) % len(WORKER_NAMES)]


def named_task(row):
    return dict(row) | {"worker_name": worker_name(row["seq"])}
