"""The model this library reasons with, printed out.

Read this before configuring anything. Which layer is binding decides what is
worth changing, and what that layer *reads* decides whether it can be satisfied at
all.

    uv run python examples/02_the_model.py
"""

from scraper import LAYERS, Layer, weakest
from scraper.layers import TRANSPORT_LAYERS, marginal_gain

print(f"{'#':>3}  {'layer':34} {'reads':8} {'do':11} ")
print("-" * 62)
for layer, info in LAYERS.items():
    print(f"{layer.value:>3}  {info.title:34} {info.trait.value:8} {info.stance.value:11}")

print()
print("Layers 2-5 are one barrier, not four:", sorted(int(x) for x in TRANSPORT_LAYERS))
print("One impersonation profile passes all four, or none of them.")

print()
print("The bound: admission is limited by the weakest layer.")
odds = {
    Layer.IP_REPUTATION: 0.05,  # a datacenter address
    Layer.TLS_FINGERPRINT: 0.99,  # a perfect Chrome profile
    Layer.BEHAVIOURAL: 0.60,
}
binding, chance = weakest(odds)  # type: ignore[misc]
print(f"  binding layer: {binding} at {chance:.0%}")

# The consequence people spend the most time ignoring.
print(f"  perfecting TLS gains: {marginal_gain(odds, Layer.TLS_FINGERPRINT, 1.0):.0%}")
print(f"  fixing the address gains: {marginal_gain(odds, Layer.IP_REPUTATION, 0.9):.0%}")
