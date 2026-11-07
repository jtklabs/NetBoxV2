import csv
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from dcim.models import Device, DeviceType, Module, ModuleType, Region, Site
from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from netbox.views.generic import (
    BulkDeleteView,
    BulkEditView,
    BulkImportView,
    ObjectDeleteView,
    ObjectEditView,
    ObjectListView,
    ObjectView,
)
from utilities.views import register_model_view

from netbox_refresh import compliance, filtersets, forms, tables
from netbox_refresh.pricing import PriceResolver
from netbox_refresh.models import (
    EFFECTIVE_EOL_ALIAS,
    effective_end_of_life_expression,
)
from netbox_refresh.models import (
    DeviceSoftware,
    ModelLifecycle,
    ReplacementPrice,
    SoftwareStandard,
    SoftwareVersion,
)


def _cisco_configured():
    """Are both halves of the Cisco Support API credential pair present?"""
    from netbox_refresh.sync import get_credentials

    client_id, client_secret = get_credentials()
    return bool(client_id and client_secret)


@register_model_view(ModelLifecycle, name='list')
class ModelLifecycleListView(ObjectListView):
    queryset = ModelLifecycle.objects.prefetch_related(
        'assigned_object_type', 'replacement_device_type', 'replacement_module_type'
    )
    table = tables.ModelLifecycleTable
    filterset = filtersets.ModelLifecycleFilterSet
    filterset_form = forms.ModelLifecycleFilterForm
    # The generic list plus a "Sync from Cisco" button — see the template.
    template_name = 'netbox_refresh/modellifecycle_list.html'

    def get_extra_context(self, request):
        return {
            'cisco_configured': _cisco_configured(),
            # The other half of the picture: hardware in NetBox that no
            # lifecycle has been written for. A list of what is configured
            # cannot show what is missing, and the missing ones are where the
            # unplanned refreshes come from.
            'unconfigured_table': tables.UnconfiguredModelTable(
                _unconfigured_models(request.GET)
            ),
        }


def _unconfigured_models(params):
    """Every device and module type without a lifecycle, as table rows.

    Narrowed by the same search box and manufacturer filter as the list
    above it, so the two halves of the page answer the same question.
    Installed counts are annotated in the query rather than looked up per
    row.
    """
    covered = {
        model: set(
            ModelLifecycle.objects.filter(assigned_object_type__model=model)
            .values_list('assigned_object_id', flat=True)
        )
        for model in ('devicetype', 'moduletype')
    }
    search = (params.get('q') or '').strip()
    manufacturers = [m for m in params.getlist('manufacturer_id') if m]

    rows = []
    for model, queryset, kind in (
        ('devicetype', DeviceType.objects.all(), 'Device type'),
        ('moduletype', ModuleType.objects.all(), 'Module type'),
    ):
        queryset = queryset.exclude(pk__in=covered[model]).select_related('manufacturer')
        queryset = queryset.annotate(installed=Count('instances'))
        if search:
            queryset = queryset.filter(
                Q(model__icontains=search) | Q(part_number__icontains=search)
                | Q(manufacturer__name__icontains=search)
            )
        if manufacturers:
            queryset = queryset.filter(manufacturer_id__in=manufacturers)
        for obj in queryset.order_by('manufacturer__name', 'model'):
            rows.append({
                'model': str(obj),
                'url': obj.get_absolute_url(),
                'manufacturer': obj.manufacturer,
                'part_number': obj.part_number or '—',
                'kind': kind,
                'installed': obj.installed,
                'add_url': '%s?%s=%d' % (
                    reverse('plugins:netbox_refresh:modellifecycle_add'),
                    'device_type' if model == 'devicetype' else 'module_type', obj.pk,
                ),
            })
    # Hardware that is actually installed first: an uncatalogued model with
    # no units is a gap on paper, one with forty units is a gap in the plan.
    rows.sort(key=lambda r: (-r['installed'], r['manufacturer'].name if r['manufacturer'] else '',
                             r['model']))
    return rows


@register_model_view(ModelLifecycle)
class ModelLifecycleView(ObjectView):
    queryset = ModelLifecycle.objects.all()

    def get_extra_context(self, request, instance):
        obj = instance.assigned_object
        devices = Module.objects.none()
        if obj is not None and obj._meta.model_name == 'devicetype':
            devices = Device.objects.filter(device_type=obj).select_related('site')[:50]
        elif obj is not None:
            devices = Module.objects.filter(module_type=obj).select_related('device')[:50]
        return {'installed': devices}


@register_model_view(ModelLifecycle, 'edit')
class ModelLifecycleEditView(ObjectEditView):
    queryset = ModelLifecycle.objects.all()
    form = forms.ModelLifecycleForm
    template_name = 'netbox_refresh/modellifecycle_edit.html'


@register_model_view(ModelLifecycle, 'delete')
class ModelLifecycleDeleteView(ObjectDeleteView):
    queryset = ModelLifecycle.objects.all()


@register_model_view(ModelLifecycle, 'bulk_edit')
class ModelLifecycleBulkEditView(BulkEditView):
    queryset = ModelLifecycle.objects.all()
    filterset = filtersets.ModelLifecycleFilterSet
    table = tables.ModelLifecycleTable
    form = forms.ModelLifecycleBulkEditForm


@register_model_view(ModelLifecycle, 'bulk_delete')
class ModelLifecycleBulkDeleteView(BulkDeleteView):
    queryset = ModelLifecycle.objects.all()
    filterset = filtersets.ModelLifecycleFilterSet
    table = tables.ModelLifecycleTable


@register_model_view(ModelLifecycle, 'bulk_import')
class ModelLifecycleBulkImportView(BulkImportView):
    queryset = ModelLifecycle.objects.all()
    model_form = forms.ModelLifecycleImportForm


# --------------------------------------------------------------------------- #
# Replacement prices
# --------------------------------------------------------------------------- #

@register_model_view(ReplacementPrice, name='list')
class ReplacementPriceListView(ObjectListView):
    queryset = ReplacementPrice.objects.select_related(
        'device_type', 'module_type', 'region', 'site'
    )
    table = tables.ReplacementPriceTable
    filterset = filtersets.ReplacementPriceFilterSet
    filterset_form = forms.ReplacementPriceFilterForm


@register_model_view(ReplacementPrice)
class ReplacementPriceView(ObjectView):
    queryset = ReplacementPrice.objects.select_related(
        'device_type', 'module_type', 'region', 'site'
    )


@register_model_view(ReplacementPrice, 'edit')
class ReplacementPriceEditView(ObjectEditView):
    queryset = ReplacementPrice.objects.all()
    form = forms.ReplacementPriceForm


@register_model_view(ReplacementPrice, 'delete')
class ReplacementPriceDeleteView(ObjectDeleteView):
    queryset = ReplacementPrice.objects.all()


@register_model_view(ReplacementPrice, 'bulk_delete')
class ReplacementPriceBulkDeleteView(BulkDeleteView):
    queryset = ReplacementPrice.objects.all()
    filterset = filtersets.ReplacementPriceFilterSet
    table = tables.ReplacementPriceTable


@register_model_view(ReplacementPrice, 'bulk_import')
class ReplacementPriceBulkImportView(BulkImportView):
    queryset = ReplacementPrice.objects.all()
    model_form = forms.ReplacementPriceImportForm


class ReplacementPriceWorksheetView(PermissionRequiredMixin, View):
    """Price one replacement model everywhere it will be needed, on one page.

    Jason's flow: "if they have a 3560 switch and the replacement is a 9350,
    when you go to the 9350 you would see all the sites that had the 3560."
    The rows are exactly that — every site holding hardware whose lifecycle
    names this model as its replacement, grouped by region, with the unit
    count so the person typing prices can see what each number multiplies.

    There is deliberately no "inherit from region" checkbox. Inheritance IS
    the absence of a site override: a site with no price of its own resolves
    to the nearest enclosing region's, so the site column shows the inherited
    figure greyed, and typing over it creates the override. Submitting a
    blank where an override exists deletes it — back to inheriting.
    """

    permission_required = 'netbox_refresh.change_replacementprice'
    template_name = 'netbox_refresh/replacementprice_worksheet.html'

    def get(self, request):
        device_type = self._device_type(request)
        context = {'device_type': device_type,
                   'picker': forms.WorksheetPickerForm(request.GET or None)}
        if device_type is not None:
            context.update(self._build(device_type))
        return render(request, self.template_name, context)

    def post(self, request):
        device_type = self._device_type(request)
        if device_type is None:
            return redirect(request.path)

        changed = 0
        for kind in ('region', 'site'):
            for key, raw_cost in request.POST.items():
                prefix = '%s_cost_' % kind
                if not key.startswith(prefix):
                    continue
                scope_pk = int(key.removeprefix(prefix))
                currency = (request.POST.get('%s_currency_%d' % (kind, scope_pk))
                            or 'USD').strip().upper()[:3]
                existing = ReplacementPrice.objects.filter(
                    device_type=device_type, **{kind: scope_pk}
                ).first()
                raw_cost = raw_cost.strip()
                if not raw_cost:
                    # Blank where an override exists: back to inheriting.
                    if existing is not None:
                        existing.delete()
                        changed += 1
                    continue
                try:
                    cost = Decimal(raw_cost.replace(',', ''))
                except InvalidOperation:
                    messages.error(request, '"%s" is not a price; row skipped.' % raw_cost)
                    continue
                if existing is not None:
                    if existing.cost != cost or existing.currency != currency:
                        existing.cost = cost
                        existing.currency = currency
                        existing.save()
                        changed += 1
                else:
                    ReplacementPrice.objects.create(
                        device_type=device_type, cost=cost, currency=currency,
                        **{kind + '_id': scope_pk},
                    )
                    changed += 1
        messages.success(request, 'Worksheet saved: %d price%s changed.'
                         % (changed, '' if changed == 1 else 's'))
        return redirect('%s?device_type=%d' % (request.path, device_type.pk))

    # ------------------------------------------------------------------ #
    def _device_type(self, request):
        source = request.POST if request.method == 'POST' else request.GET
        pk = source.get('device_type')
        if not pk:
            return None
        return DeviceType.objects.filter(pk=pk).first()

    def _build(self, device_type):
        feeders = list(ModelLifecycle.objects.filter(
            replacement_device_type=device_type
        ).prefetch_related('assigned_object_type'))
        feeder_type_ids = [f.assigned_object_id for f in feeders
                           if f.assigned_object_type.model == 'devicetype']

        site_units = defaultdict(int)
        for row in Device.objects.filter(
            device_type_id__in=feeder_type_ids
        ).values('site_id').annotate(n=Count('pk')):
            site_units[row['site_id']] = row['n']
        sites = Site.objects.filter(pk__in=site_units).select_related('region')

        prices = {
            ('site', p.site_id) if p.site_id else ('region', p.region_id): p
            for p in ReplacementPrice.objects.filter(device_type=device_type)
        }
        all_regions = Region.objects.in_bulk()

        def label(region_id):
            parts, seen = [], set()
            while region_id is not None and region_id not in seen:
                seen.add(region_id)
                region = all_regions.get(region_id)
                if region is None:
                    break
                parts.append(region.name)
                region_id = region.parent_id
            return ' / '.join(reversed(parts))

        def inherited(site):
            """What this site resolves to with no override of its own."""
            region_id = site.region_id
            seen = set()
            while region_id is not None and region_id not in seen:
                seen.add(region_id)
                price = prices.get(('region', region_id))
                if price is not None:
                    return '%s %s (from %s)' % (
                        price.cost, price.currency, all_regions[region_id].name)
                region_id = all_regions.get(region_id) and all_regions[region_id].parent_id
            return 'baseline'

        # Region rows: every region an involved site sits in, plus every
        # ancestor — so "set EMEA once" is possible even when every site
        # lives deeper in the tree. Regions that already carry a price appear
        # too, so an existing entry can always be revised or cleared.
        region_ids = set()
        for site in sites:
            region_id = site.region_id
            while region_id is not None and region_id not in region_ids:
                region_ids.add(region_id)
                region_id = all_regions[region_id].parent_id
        region_ids.update(pk for kind, pk in prices if kind == 'region')

        region_rows = []
        for region_id in region_ids:
            price = prices.get(('region', region_id))
            region_rows.append({
                'region': all_regions[region_id],
                'label': label(region_id),
                'cost': price.cost if price else '',
                'currency': price.currency if price else 'USD',
            })
        region_rows.sort(key=lambda r: r['label'])

        groups = defaultdict(list)
        for site in sorted(sites, key=lambda s: s.name):
            price = prices.get(('site', site.pk))
            groups[label(site.region_id) or '(no region)'].append({
                'site': site,
                'units': site_units[site.pk],
                'cost': price.cost if price else '',
                'currency': price.currency if price else 'USD',
                'inherited': inherited(site),
            })
        site_groups = [{'label': k, 'sites': v} for k, v in sorted(groups.items())]

        return {
            'feeders': feeders,
            'region_rows': region_rows,
            'site_groups': site_groups,
            'total_units': sum(site_units.values()),
        }


def _sites_in_scope(regions, sites):
    """The sites a report is scoped to, from either filter or both.

    Regions nest, so selecting EMEA has to mean every site under it and not
    just the ones parented directly to it — otherwise the report quietly
    reports on a fraction of the estate and reads as though the rest owns no
    hardware.

    Given both, the intersection is used: two filters shown together read as
    narrowing, and a site outside the chosen regions is a contradiction rather
    than an addition. Returning None means "no scope", which is not the same
    as an empty list — that would be "no sites at all" and would count nothing.
    """
    if not regions and not sites:
        return None
    if not regions:
        return list(sites)

    region_ids = {
        pk for region in regions
        for pk in region.get_descendants(include_self=True).values_list('pk', flat=True)
    }
    within = Site.objects.filter(region_id__in=region_ids)
    if sites:
        chosen = {s.pk for s in sites}
        return [s for s in within if s.pk in chosen]
    return list(within)


def _money(per_currency):
    """One cell for a currency->amount mapping: '12,000.00 USD + 9,500.00 EUR'.

    Never converted or combined — any exchange rate hardcoded here would be
    wrong by the time it was read, so each currency keeps its own number.
    """
    if not per_currency:
        return '—'
    return ' + '.join(
        '{:,.2f} {}'.format(amount, currency)
        for currency, amount in sorted(per_currency.items())
    )


def _cell(value):
    """A CSV cell: dates as ISO, money as a plain number, dashes as empty."""
    if value is None or value == '—':
        return ''
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return '%.2f' % value
    return str(value)


def _site_rows(site_agg):
    """The per-site cost breakdown: what each site has to replace, in money."""
    sites = Site.objects.select_related('region').in_bulk(
        [site_id for site_id in site_agg if site_id is not None]
    )
    rows = []
    for site_id, agg in site_agg.items():
        site = sites.get(site_id)
        region = site.region if site else None
        rows.append({
            'site': site.name if site else '(no site)',
            'site_url': site.get_absolute_url() if site else None,
            'region': region.name if region else '—',
            'region_url': region.get_absolute_url() if region else None,
            'models': agg['models'],
            'units': agg['units'],
            'total': _money(agg['totals']),
            'unpriced': agg['unpriced'] or '',
        })
    rows.sort(key=lambda r: (r['site_url'] is None, r['site']))
    return rows


def _region_rows(region_agg):
    """The per-region cost breakdown, labelled with each region's tree path."""
    all_regions = Region.objects.in_bulk()

    def label(region_id):
        parts = []
        seen = set()
        while region_id is not None and region_id not in seen:
            seen.add(region_id)
            region = all_regions.get(region_id)
            if region is None:
                break
            parts.append(region.name)
            region_id = region.parent_id
        return ' / '.join(reversed(parts))

    rows = []
    for region_id, agg in region_agg.items():
        region = all_regions.get(region_id)
        rows.append({
            'region': label(region_id) if region else '(no region)',
            'region_url': region.get_absolute_url() if region else None,
            'units': agg['units'],
            'total': _money(agg['totals']),
            'unpriced': agg['unpriced'] or '',
        })
    # Alphabetical by path keeps siblings together; the regionless bucket last.
    rows.sort(key=lambda r: (r['region_url'] is None, r['region']))
    return rows


class RefreshReportView(PermissionRequiredMixin, View):
    """Hardware reaching a lifecycle milestone in a window, with replacement cost.

    Installed counts are gathered per model and site with two aggregate
    queries, so the report stays cheap as the estate grows — and priced per
    site, because ReplacementPrice lets the same model cost different money
    in different places.

    Two breakdowns of the same numbers. By model answers "what is going end
    of life and what will it cost"; by site answers "what does each site have
    to replace", which is the list a site's refresh is planned from. Either
    can be taken away as CSV (`?export=csv`), the same rows the page shows.
    """

    permission_required = 'netbox_refresh.view_modellifecycle'
    template_name = 'netbox_refresh/refresh_report.html'

    def get(self, request):
        form = forms.RefreshReportForm(request.GET or None)
        form.is_valid()
        data = form.cleaned_data if form.is_bound else {}

        report = self._build(data)
        breakdown = data.get('breakdown') or 'model'
        if request.GET.get('export') == 'csv':
            return self._csv(report, breakdown)

        return render(request, self.template_name, {
            'form': form,
            'breakdown': breakdown,
            'table': tables.RefreshReportTable(report['rows']),
            'site_table': tables.SiteRefreshTable(report['site_rows']),
            'region_table': tables.RegionCostTable(_region_rows(report['region_agg'])),
            'site_cost_table': tables.SiteCostTable(_site_rows(report['site_agg'])),
            'row_count': len(report['rows']),
            'site_count': len(report['site_agg']),
            'total_units': report['total_units'],
            'totals': dict(report['totals']),
            'missing_cost': report['models_with_gaps'],
            'unpriced_units': report['unpriced_units'],
            'date_label': report['date_label'],
            'export_query': request.GET.urlencode(),
        })

    def _build(self, data):
        date_field = data.get('date_field') or EFFECTIVE_EOL_ALIAS
        after = data.get('after')
        before = data.get('before')
        manufacturers = data.get('manufacturer')
        sites = _sites_in_scope(data.get('region'), data.get('site'))

        # Annotated unconditionally so the report can filter and sort on the
        # effective end-of-life the same way it does a stored column; the
        # model property cannot be used in a query.
        queryset = ModelLifecycle.objects.prefetch_related(
            'assigned_object_type', 'replacement_device_type', 'replacement_module_type'
        ).annotate(
            **{EFFECTIVE_EOL_ALIAS: effective_end_of_life_expression()}
        ).filter(**{'%s__isnull' % date_field: False})
        if after:
            queryset = queryset.filter(**{'%s__gte' % date_field: after})
        if before:
            queryset = queryset.filter(**{'%s__lte' % date_field: before})

        records = list(queryset)

        device_type_ids = [r.assigned_object_id for r in records
                           if r.assigned_object_type.model == 'devicetype']
        module_type_ids = [r.assigned_object_id for r in records
                           if r.assigned_object_type.model == 'moduletype']

        # Counts are per (model, site), not just per model: the same box does
        # not cost the same in every country, so money can only be summed
        # after each site's units are priced at that site's rate.
        device_site_counts = defaultdict(dict)   # type_id -> {site_id: n}
        if device_type_ids:
            device_qs = Device.objects.filter(device_type_id__in=device_type_ids)
            if sites:
                device_qs = device_qs.filter(site__in=sites)
            for row in device_qs.values('device_type_id', 'site_id').annotate(n=Count('pk')):
                device_site_counts[row['device_type_id']][row['site_id']] = row['n']

        module_site_counts = defaultdict(dict)   # type_id -> {site_id: n}
        if module_type_ids:
            module_qs = Module.objects.filter(module_type_id__in=module_type_ids)
            if sites:
                module_qs = module_qs.filter(device__site__in=sites)
            for row in module_qs.values('module_type_id', 'device__site_id').annotate(n=Count('pk')):
                module_site_counts[row['module_type_id']][row['device__site_id']] = row['n']

        type_cache = {
            'devicetype': DeviceType.objects.in_bulk(device_type_ids),
            'moduletype': ModuleType.objects.in_bulk(module_type_ids),
        }
        involved_site_ids = {
            site_id
            for per_site in list(device_site_counts.values()) + list(module_site_counts.values())
            for site_id in per_site
        }
        # With regions, which the by-site rows name on every line.
        site_cache = Site.objects.select_related('region').in_bulk(involved_site_ids)
        resolver = PriceResolver([r.pk for r in records])

        rows = []
        site_rows = []
        totals = defaultdict(lambda: 0)
        total_units = 0
        models_with_gaps = 0
        unpriced_units = 0
        # region_id (None = siteless bucket) -> aggregation for the breakdown
        region_agg = defaultdict(lambda: {'units': 0, 'unpriced': 0,
                                          'totals': defaultdict(lambda: 0)})
        # site_id (None = siteless bucket) -> the same, plus how many models
        site_agg = defaultdict(lambda: {'units': 0, 'unpriced': 0, 'models': 0,
                                        'totals': defaultdict(lambda: 0)})
        for record in records:
            model_name = record.assigned_object_type.model
            obj = type_cache.get(model_name, {}).get(record.assigned_object_id)
            if obj is None:
                continue
            if manufacturers and obj.manufacturer_id not in {m.pk for m in manufacturers}:
                continue
            per_site = (device_site_counts if model_name == 'devicetype'
                        else module_site_counts)[obj.pk]
            installed = sum(per_site.values())
            if sites and not installed:
                continue  # nothing of this model at the selected sites

            replacement = record.replacement
            milestone_date = getattr(record, date_field)
            extended = defaultdict(lambda: 0)    # currency -> amount, this model
            model_unpriced = 0
            for site_id, count in per_site.items():
                site = site_cache.get(site_id)
                hit = resolver.resolve(record, site)
                bucket = region_agg[site.region_id if site else None]
                bucket['units'] += count
                per_site_bucket = site_agg[site_id if site else None]
                per_site_bucket['units'] += count
                per_site_bucket['models'] += 1
                amount = hit.cost * count if hit is not None else None
                if hit is None:
                    model_unpriced += count
                    bucket['unpriced'] += count
                    per_site_bucket['unpriced'] += count
                else:
                    extended[hit.currency] += amount
                    totals[hit.currency] += amount
                    bucket['totals'][hit.currency] += amount
                    per_site_bucket['totals'][hit.currency] += amount
                # One row per site and model: the site's own refresh list,
                # priced at the site's own rate.
                site_rows.append({
                    'site': site.name if site else '(no site)',
                    'site_url': site.get_absolute_url() if site else None,
                    'region': site.region.name if site and site.region else '—',
                    'region_url': site.region.get_absolute_url() if site and site.region else None,
                    'model': str(obj),
                    'url': record.get_absolute_url(),
                    'manufacturer': obj.manufacturer,
                    'part_number': obj.part_number or '—',
                    'milestone_date': milestone_date,
                    'installed': count,
                    'replacement': replacement or '—',
                    'replacement_url': replacement.get_absolute_url() if replacement else None,
                    'unit_cost': '{:,.2f} {}'.format(hit.cost, hit.currency) if hit else '—',
                    'unit_cost_amount': hit.cost if hit else None,
                    'currency': hit.currency if hit else '',
                    'extended_cost': '{:,.2f} {}'.format(amount, hit.currency) if hit else '—',
                    'extended_cost_amount': amount,
                })
            if model_unpriced:
                models_with_gaps += 1
                unpriced_units += model_unpriced
            total_units += installed

            regional = resolver.price_count(record)
            if record.replacement_cost is not None:
                unit_cost = '%s %s' % (record.replacement_cost, record.currency)
                if regional:
                    unit_cost += ' (+%d regional)' % regional
            else:
                unit_cost = '%d regional price%s' % (regional, 's' if regional != 1 else '') \
                    if regional else '—'

            rows.append({
                'model': str(obj),
                'url': record.get_absolute_url(),
                'manufacturer': obj.manufacturer,
                'part_number': obj.part_number or '—',
                'milestone_date': milestone_date,
                'installed': installed,
                'replacement': replacement or '—',
                'replacement_url': replacement.get_absolute_url() if replacement else None,
                'unit_cost': unit_cost,
                'extended_cost': _money(extended),
            })

        rows.sort(key=lambda r: (r['milestone_date'] is None, r['milestone_date']))
        # Site by site, each site's list soonest first; the siteless bucket last.
        site_rows.sort(key=lambda r: (r['site_url'] is None, r['site'],
                                      r['milestone_date'] is None, r['milestone_date'],
                                      r['model']))

        return {
            'rows': rows,
            'site_rows': site_rows,
            'region_agg': region_agg,
            'site_agg': site_agg,
            'totals': totals,
            'total_units': total_units,
            'models_with_gaps': models_with_gaps,
            'unpriced_units': unpriced_units,
            'date_label': dict(forms.RefreshReportForm.DATE_FIELD_CHOICES).get(date_field),
        }

    # Column order and headings for the two exports. Money is split into an
    # amount and a currency where a row has exactly one price -- the by-site
    # rows -- so a spreadsheet can sum it; a by-model row can span several
    # currencies and keeps the page's "12,000.00 USD + 9,500.00 EUR" text.
    MODEL_COLUMNS = (
        ('model', 'Hardware model'), ('manufacturer', 'Manufacturer'),
        ('part_number', 'Part number'), ('milestone_date', 'Milestone date'),
        ('installed', 'Installed'), ('replacement', 'Replacement'),
        ('unit_cost', 'Unit cost'), ('extended_cost', 'Extended cost'),
    )
    SITE_COLUMNS = (
        ('site', 'Site'), ('region', 'Region'), ('model', 'Hardware model'),
        ('manufacturer', 'Manufacturer'), ('part_number', 'Part number'),
        ('milestone_date', 'Milestone date'), ('installed', 'Units'),
        ('replacement', 'Replacement'), ('unit_cost_amount', 'Unit cost'),
        ('currency', 'Currency'), ('extended_cost_amount', 'Extended cost'),
    )

    def _csv(self, report, breakdown):
        """The rows the page shows, as a file. Nothing is summarised: the
        totals are a spreadsheet's job once it has the rows."""
        columns = self.SITE_COLUMNS if breakdown == 'site' else self.MODEL_COLUMNS
        rows = report['site_rows'] if breakdown == 'site' else report['rows']
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = (
            'attachment; filename="refresh-report-by-%s-%s.csv"'
            % (breakdown, date.today().isoformat())
        )
        writer = csv.writer(response)
        writer.writerow(['Milestone'] + [heading for _key, heading in columns])
        for row in rows:
            writer.writerow([report['date_label']] + [_cell(row.get(key)) for key, _ in columns])
        return response


# --------------------------------------------------------------------------- #
# Software versions
# --------------------------------------------------------------------------- #

@register_model_view(SoftwareVersion, name='list')
class SoftwareVersionListView(ObjectListView):
    queryset = SoftwareVersion.objects.select_related('platform')
    table = tables.SoftwareVersionTable
    filterset = filtersets.SoftwareVersionFilterSet
    filterset_form = forms.SoftwareVersionFilterForm


@register_model_view(SoftwareVersion)
class SoftwareVersionView(ObjectView):
    queryset = SoftwareVersion.objects.select_related('platform')

    def get_extra_context(self, request, instance):
        devices = DeviceSoftware.objects.filter(
            software_version=instance
        ).select_related('device', 'device__site')[:100]
        standards = instance.approved_by_standards.prefetch_related(
            'device_types', 'platforms'
        )
        return {'running_on': devices, 'standards': standards}


@register_model_view(SoftwareVersion, 'edit')
class SoftwareVersionEditView(ObjectEditView):
    queryset = SoftwareVersion.objects.all()
    form = forms.SoftwareVersionForm


@register_model_view(SoftwareVersion, 'delete')
class SoftwareVersionDeleteView(ObjectDeleteView):
    queryset = SoftwareVersion.objects.all()


@register_model_view(SoftwareVersion, 'bulk_edit')
class SoftwareVersionBulkEditView(BulkEditView):
    queryset = SoftwareVersion.objects.all()
    filterset = filtersets.SoftwareVersionFilterSet
    table = tables.SoftwareVersionTable
    form = forms.SoftwareVersionBulkEditForm


@register_model_view(SoftwareVersion, 'bulk_delete')
class SoftwareVersionBulkDeleteView(BulkDeleteView):
    queryset = SoftwareVersion.objects.all()
    filterset = filtersets.SoftwareVersionFilterSet
    table = tables.SoftwareVersionTable


@register_model_view(SoftwareVersion, 'bulk_import')
class SoftwareVersionBulkImportView(BulkImportView):
    queryset = SoftwareVersion.objects.all()
    model_form = forms.SoftwareVersionImportForm


# --------------------------------------------------------------------------- #
# Software standards
# --------------------------------------------------------------------------- #

@register_model_view(SoftwareStandard, name='list')
class SoftwareStandardListView(ObjectListView):
    queryset = SoftwareStandard.objects.prefetch_related(
        'device_types', 'platforms', 'approved_versions', 'preferred_version'
    )
    table = tables.SoftwareStandardTable
    filterset = filtersets.SoftwareStandardFilterSet
    filterset_form = forms.SoftwareStandardFilterForm


@register_model_view(SoftwareStandard)
class SoftwareStandardView(ObjectView):
    queryset = SoftwareStandard.objects.prefetch_related(
        'device_types', 'platforms', 'approved_versions'
    )

    def get_extra_context(self, request, instance):
        """Show related standards — anything sharing part of this one's scope —
        so supersession is visible in place."""
        history = SoftwareStandard.objects.filter(
            Q(device_types__in=instance.device_types.all())
            | Q(platforms__in=instance.platforms.all())
        ).exclude(pk=instance.pk).distinct().order_by('-valid_from')
        return {'history': history}


@register_model_view(SoftwareStandard, 'edit')
class SoftwareStandardEditView(ObjectEditView):
    queryset = SoftwareStandard.objects.all()
    form = forms.SoftwareStandardForm


@register_model_view(SoftwareStandard, 'delete')
class SoftwareStandardDeleteView(ObjectDeleteView):
    queryset = SoftwareStandard.objects.all()


@register_model_view(SoftwareStandard, 'bulk_edit')
class SoftwareStandardBulkEditView(BulkEditView):
    queryset = SoftwareStandard.objects.all()
    filterset = filtersets.SoftwareStandardFilterSet
    table = tables.SoftwareStandardTable
    form = forms.SoftwareStandardBulkEditForm


@register_model_view(SoftwareStandard, 'bulk_delete')
class SoftwareStandardBulkDeleteView(BulkDeleteView):
    queryset = SoftwareStandard.objects.all()
    filterset = filtersets.SoftwareStandardFilterSet
    table = tables.SoftwareStandardTable


# --------------------------------------------------------------------------- #
# Per-device running software
# --------------------------------------------------------------------------- #

@register_model_view(DeviceSoftware, name='list')
class DeviceSoftwareListView(ObjectListView):
    queryset = DeviceSoftware.objects.select_related(
        'device', 'device__site', 'software_version', 'software_version__platform'
    )
    table = tables.DeviceSoftwareTable
    filterset = filtersets.DeviceSoftwareFilterSet
    filterset_form = forms.DeviceSoftwareFilterForm


@register_model_view(DeviceSoftware)
class DeviceSoftwareView(ObjectView):
    queryset = DeviceSoftware.objects.select_related(
        'device', 'software_version', 'software_version__platform'
    )


@register_model_view(DeviceSoftware, 'edit')
class DeviceSoftwareEditView(ObjectEditView):
    queryset = DeviceSoftware.objects.all()
    form = forms.DeviceSoftwareForm


@register_model_view(DeviceSoftware, 'delete')
class DeviceSoftwareDeleteView(ObjectDeleteView):
    queryset = DeviceSoftware.objects.all()


@register_model_view(DeviceSoftware, 'bulk_edit')
class DeviceSoftwareBulkEditView(BulkEditView):
    queryset = DeviceSoftware.objects.all()
    filterset = filtersets.DeviceSoftwareFilterSet
    table = tables.DeviceSoftwareTable
    form = forms.DeviceSoftwareBulkEditForm


@register_model_view(DeviceSoftware, 'bulk_delete')
class DeviceSoftwareBulkDeleteView(BulkDeleteView):
    queryset = DeviceSoftware.objects.all()
    filterset = filtersets.DeviceSoftwareFilterSet
    table = tables.DeviceSoftwareTable


@register_model_view(DeviceSoftware, 'bulk_import')
class DeviceSoftwareBulkImportView(BulkImportView):
    queryset = DeviceSoftware.objects.all()
    model_form = forms.DeviceSoftwareImportForm


class ComplianceReportView(PermissionRequiredMixin, View):
    """Which devices run approved code, which do not, and which we cannot say.

    The report iterates DEVICES, not software records, so a device nobody has
    ever scanned shows up as Unknown instead of quietly not existing. Exempt
    devices are shown as exempt rather than filtered out, for the same reason.

    Status is derived rather than stored, so status filtering happens in Python
    after the rows are built. Everything that can be pushed into SQL — site,
    role, platform, manufacturer, device type — is applied to the device
    queryset first, which is what keeps that affordable.
    """

    permission_required = 'netbox_refresh.view_devicesoftware'
    template_name = 'netbox_refresh/compliance_report.html'

    def get(self, request):
        form = forms.ComplianceReportForm(request.GET or None)
        form.is_valid()
        data = form.cleaned_data if form.is_bound else {}

        devices = Device.objects.select_related(
            'site', 'device_type', 'device_type__manufacturer', 'platform', 'role'
        )
        if data.get('site'):
            devices = devices.filter(site__in=data['site'])
        if data.get('role'):
            devices = devices.filter(role__in=data['role'])
        if data.get('platform'):
            devices = devices.filter(platform__in=data['platform'])
        if data.get('manufacturer'):
            devices = devices.filter(device_type__manufacturer__in=data['manufacturer'])
        if data.get('device_type'):
            devices = devices.filter(device_type__in=data['device_type'])

        rows = compliance.device_compliance_rows(devices, on_date=data.get('as_of'))
        summary = compliance.summarise(rows)

        wanted = data.get('status')
        if wanted:
            rows = [row for row in rows if row['status'] in wanted]

        rows.sort(key=lambda row: (row['status'], str(row['device'])))
        stale_count = sum(1 for row in rows if row['is_stale'])

        return render(request, self.template_name, {
            'form': form,
            'table': tables.ComplianceReportTable(rows),
            'summary': summary,
            'row_count': len(rows),
            'total_devices': sum(item['count'] for item in summary),
            'stale_count': stale_count,
            'as_of': data.get('as_of'),
            'filtered': bool(wanted),
        })


class VersionRollupView(PermissionRequiredMixin, View):
    """Devices per version per model — what an upgrade campaign is planned from."""

    permission_required = 'netbox_refresh.view_devicesoftware'
    template_name = 'netbox_refresh/version_rollup.html'

    def get(self, request):
        form = forms.ComplianceReportForm(request.GET or None)
        form.is_valid()
        data = form.cleaned_data if form.is_bound else {}

        devices = Device.objects.select_related(
            'site', 'device_type', 'device_type__manufacturer', 'platform'
        )
        if data.get('site'):
            devices = devices.filter(site__in=data['site'])
        if data.get('role'):
            devices = devices.filter(role__in=data['role'])
        if data.get('platform'):
            devices = devices.filter(platform__in=data['platform'])
        if data.get('manufacturer'):
            devices = devices.filter(device_type__manufacturer__in=data['manufacturer'])
        if data.get('device_type'):
            devices = devices.filter(device_type__in=data['device_type'])

        rows = compliance.device_compliance_rows(devices, on_date=data.get('as_of'))

        # Group on (device type, version). Compliance is constant within a group:
        # the standard is resolved from device type then platform, both of which
        # are fixed inside the group.
        groups = {}
        for row in rows:
            device = row['device']
            key = (str(device.device_type), str(device.platform or '—'), row['version'] or '—')
            bucket = groups.setdefault(key, {
                'device_type': key[0],
                'platform': key[1],
                'version': key[2],
                'version_url': (row['record'].software_version.get_absolute_url()
                                if row['record'] and row['record'].software_version_id else None),
                'status_label': row['status_label'],
                'status_color': row['status_color'],
                'count': 0,
            })
            bucket['count'] += 1

        table_rows = sorted(
            groups.values(), key=lambda item: (-item['count'], item['device_type'])
        )
        return render(request, self.template_name, {
            'form': form,
            'table': tables.VersionRollupTable(table_rows),
            'group_count': len(table_rows),
            'total_devices': sum(item['count'] for item in table_rows),
        })


class CiscoSyncView(PermissionRequiredMixin, View):
    """Kick off the Cisco EoX sync as a background job."""

    permission_required = 'netbox_refresh.change_modellifecycle'

    def post(self, request):
        from netbox_refresh.jobs import CiscoEoxSyncJob

        if not _cisco_configured():
            # Say so here rather than enqueueing a job whose only possible
            # outcome is an error entry under Jobs a minute later.
            messages.error(
                request,
                'Cisco Support API credentials are not configured '
                '(CISCO_CLIENT_ID / CISCO_CLIENT_SECRET), so the sync cannot run.',
            )
            return redirect('plugins:netbox_refresh:modellifecycle_list')
        try:
            job = CiscoEoxSyncJob.enqueue(user=request.user)
        except Exception as exc:  # noqa: BLE001 - surface scheduling failures in the UI
            messages.error(request, 'Could not start the Cisco EoX sync: %s' % exc)
        else:
            messages.success(
                request,
                'Cisco EoX sync started (job #%s). Progress is under Operations › Jobs.'
                % job.pk,
            )
        return redirect('plugins:netbox_refresh:modellifecycle_list')
