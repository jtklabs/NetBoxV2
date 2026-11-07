"""Resolving what refreshing a unit actually costs at a given site.

The refresh report renders a row per outgoing hardware model but sums money
per site, because the replacement being bought does not cost the same in
every country. Doing that naively is a price lookup per (model, site) pair
with a region-tree walk inside — this module loads everything once and
answers from dicts, the same shape as compliance.StandardResolver.

Prices are keyed to the REPLACEMENT model (the thing being purchased — see
ReplacementPrice), so resolution for an outgoing lifecycle goes through its
replacement_device_type / replacement_module_type. Precedence, most specific
wins:

    replacement's site price  >  its nearest enclosing region's price
                              >  the lifecycle's baseline replacement_cost

"Nearest enclosing" walks the region tree upward from the site's region, so a
price on EMEA covers a London site under EMEA North unless EMEA North (or
London itself) has its own. A lifecycle with no replacement model recorded
can only ever resolve to its baseline — you cannot price "the new one"
before naming the new one.
"""

from dataclasses import dataclass
from decimal import Decimal

from dcim.models import Region

from netbox_refresh.models import ReplacementPrice

__all__ = ('PriceHit', 'PriceResolver')


@dataclass(frozen=True)
class PriceHit:
    """One resolved unit price, and where it came from."""

    cost: Decimal
    currency: str
    # 'site', 'region', or 'base' — the report says which prices are overrides
    # so a surprising number can be traced to the record that produced it.
    source: str


def _model_key(price):
    if price.device_type_id:
        return ('devicetype', price.device_type_id)
    return ('moduletype', price.module_type_id)


def _replacement_key(lifecycle):
    """The lifecycle's replacement model, as a lookup key. None when unset."""
    if lifecycle.replacement_device_type_id:
        return ('devicetype', lifecycle.replacement_device_type_id)
    if lifecycle.replacement_module_type_id:
        return ('moduletype', lifecycle.replacement_module_type_id)
    return None


class PriceResolver:
    """Bulk price resolution for a set of lifecycles, three queries total."""

    def __init__(self, lifecycle_ids):
        from netbox_refresh.models import ModelLifecycle

        replacement_keys = set()
        for lifecycle in ModelLifecycle.objects.filter(pk__in=lifecycle_ids).only(
            'pk', 'replacement_device_type_id', 'replacement_module_type_id'
        ):
            key = _replacement_key(lifecycle)
            if key is not None:
                replacement_keys.add(key)

        device_type_ids = [pk for kind, pk in replacement_keys if kind == 'devicetype']
        module_type_ids = [pk for kind, pk in replacement_keys if kind == 'moduletype']

        self._by_site = {}     # (model_key, site_id) -> PriceHit
        self._by_region = {}   # (model_key, region_id) -> PriceHit
        self._counts = {}      # model_key -> number of prices
        prices = ReplacementPrice.objects.none()
        if device_type_ids or module_type_ids:
            from django.db.models import Q
            prices = ReplacementPrice.objects.filter(
                Q(device_type_id__in=device_type_ids)
                | Q(module_type_id__in=module_type_ids)
            )
        for price in prices:
            key = _model_key(price)
            hit_source = 'site' if price.site_id else 'region'
            hit = PriceHit(cost=price.cost, currency=price.currency, source=hit_source)
            if price.site_id:
                self._by_site[(key, price.site_id)] = hit
            else:
                self._by_region[(key, price.region_id)] = hit
            self._counts[key] = self._counts.get(key, 0) + 1

        # The whole region tree as a parent map. Loading all of it is cheaper
        # than being clever: region tables are small (countries and areas, not
        # devices), and a dict walk per lookup beats a query per lookup.
        self._parents = dict(Region.objects.values_list('id', 'parent_id'))

    def price_count(self, lifecycle) -> int:
        """Prices recorded for this lifecycle's replacement — 0 means baseline only."""
        key = _replacement_key(lifecycle)
        return self._counts.get(key, 0) if key else 0

    def resolve(self, lifecycle, site) -> PriceHit | None:
        """The unit price for refreshing this lifecycle's model at this site.

        None means no price is known anywhere — not even a baseline — which
        the report has to surface rather than treat as zero: a missing price
        understates the total silently, and an understated refresh budget is
        the expensive kind of wrong.
        """
        key = _replacement_key(lifecycle)
        if key is not None:
            hit = self._by_site.get((key, site.pk if site else None))
            if hit is not None:
                return hit

            region_id = site.region_id if site else None
            seen = set()
            while region_id is not None and region_id not in seen:
                seen.add(region_id)
                hit = self._by_region.get((key, region_id))
                if hit is not None:
                    return hit
                region_id = self._parents.get(region_id)

        if lifecycle.replacement_cost is not None:
            return PriceHit(
                cost=lifecycle.replacement_cost,
                currency=lifecycle.currency,
                source='base',
            )
        return None
