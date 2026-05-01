import logging
from collections import OrderedDict
from datetime import datetime
from importlib import import_module
import operator as op
import re

logger = logging.getLogger(__name__)


from django.conf import settings
from django.db import models
from django.db.models import Q, Func, Count, Sum, Max, Min, Avg, Variance, StdDev
import pandas as pd
import pytz

from .dates import Dates
from .filters import Filter as OrmFilter

SAFE_OPS = {
    '+': op.add,
    '-': op.sub,
    '*': op.mul,
    '/': op.truediv,
}

FIELD_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*(__[a-zA-Z_][a-zA-Z0-9_]*)*$')


def build_f_expression(on_field):
    """Build a Django ``F``/arithmetic expression from a list of field
    references and operators (e.g. ``['price', '*', 'qty']``)."""
    result = None
    current_op = None
    for key in on_field:
        if key in SAFE_OPS:
            current_op = SAFE_OPS[key]
        else:
            if not FIELD_PATTERN.match(key):
                raise ValueError(f"Invalid field name: '{key}'")
            f_obj = models.F(key)
            if result is None:
                result = f_obj
            elif current_op:
                result = current_op(result, f_obj)
                current_op = None
            else:
                raise ValueError(f"Missing operator before field '{key}'")
    return result

USE_TZ = settings.USE_TZ

DELTA_REGEX = re.compile(r'^(?P<delta_int>-?\d*)(?P<delta_time>d{1}|m{1}|y{1})$')

CALC = {
    'avg': Avg,
    'sum': Sum,
    'count': Count,
    'min': Min,
    'max': Max,
    'variance': Variance,
    'std dev': StdDev
}


class Year(Func):
    template = """DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%Y")"""
    output_field = models.CharField()


class Quarter(Func):
    template = """CONCAT(DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%Y"),
        " ",
        CEIL(EXTRACT(MONTH from %(expressions)s + INTERVAL %(interval)s SECOND)/3),
        "Q"
    )"""
    output_field = models.CharField()


class Month(Func):
    template = """DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%Y-%%%%m")"""
    output_field = models.CharField()


class Day(Func):
    template = """DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%Y-%%%%m-%%%%d")"""
    output_field = models.CharField()


class WeekDay(Func):
    template = """DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%W")"""
    output_field = models.CharField()


class WeekDayHour(Func):
    template = """DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%W %%%%H")"""
    output_field = models.CharField()


class Hour(Func):
    template = """DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%Y-%%%%m-%%%%d %%%%H")"""
    output_field = models.CharField()


EXTRACT = {
    'day': Day,
    'hour': Hour,
    'month': Month,
    'quarter': Quarter,
    'weekday': WeekDay,
    'weekdayhour': WeekDayHour,
    'year': Year
}


def get_model(model_name):
    """Resolve a model class from a ``"module_class"`` string by importing
    ``modules.<module>.models``."""
    module, _class_name = model_name.split('_')

    try:
        model = getattr(
            import_module('modules.{}.models'.format(module.lower())),
            _class_name
        )
    except ImportError:
        return

    return model


async def aggregate(
    model, on_field, calc, timezone, distinct
):
    """Run a single aggregation (``count``/``sum``/``avg``/...) over a model
    and return a scalar ``{calc_name: value}`` dict."""
    calc = CALC[calc[0]]

    if isinstance(on_field, list):
        aggregation = build_f_expression(on_field)
    else:
        aggregation = on_field

    if on_field == 'id':
        data = await model.aaggregate(aggregated_total=calc(aggregation, distinct=distinct))

    elif isinstance(on_field, list):
        data = await model.aaggregate(
            aggregated_total=calc(
                aggregation,
                output_field=models.FloatField(),
                distinct=distinct
            )
        )

    elif on_field:
        data = await model.values(
            on_field
        ).aaggregate(aggregated_total=calc(aggregation, distinct=distinct))

    return {'total': data['aggregated_total']}


async def group_by(
    queryset, on_field, additional_fields, calc, groups, order,
    timezone, date_group, limit, distinct, model
):
    """Group rows by ``groups`` (and optionally a ``date_group`` truncated
    to year/quarter/month/...), apply each formula in ``calc``, and return
    the rows alongside any ``choices`` metadata for the grouped fields."""
    fields = {}
    if groups:
        for key in groups:
            tmp_model = model
            related_info = key.split('__')
            for i, nome_campo in enumerate(related_info):
                try:
                    field = tmp_model._meta.get_field(nome_campo)
                    if i == len(related_info) - 1:  # last segment of the path
                        if field.choices:
                            fields[key] = {}
                            for choice in field.choices:
                                fields[key][choice[0]] = choice[1]
                    else:
                        tmp_model = field.related_model  # Walk to the next related model
                except Exception as e:
                    logger.warning('calc: failed to resolve field: %s', e)
                    break

    if not on_field:
        on_field = 'id'

    if not groups:
        groups = []

    if date_group:
        date_field = date_group['field']
        group_by = date_group['group_by']
        extracted = 'extracted_' + date_field
        truncate = EXTRACT[group_by.lower()]

        if USE_TZ:
            # Adjust the DB lookup according to the timezone
            seconds_offset = timezone.utcoffset(datetime.utcnow()).total_seconds()
            exp = {extracted: truncate(date_field, tzinfo=timezone, interval=seconds_offset)}
        else:
            exp = {extracted: truncate(date_field, tzinfo=timezone, interval=0)}
            # exp = {extracted: truncate(date_field, interval=0)}

        queryset = queryset.annotate(
            **exp
        )

        if order:
            order.insert(0, extracted)
        groups.insert(0, extracted)

    queryset = queryset.values(*groups)

    formulas = {}
    keys = []

    if isinstance(on_field, list):
        aggregation = build_f_expression(on_field)
    else:
        aggregation = on_field

    for formula in calc:
        calc = CALC[formula]
        keys.append(formula)

        # Sum returns Decimal but we need Float/Int for charts to render correctly
        if formula == 'sum':
            formulas[formula] = calc(
                aggregation,
                output_field=models.FloatField(),
                distinct=distinct
            )
        else:
            formulas[formula] = calc(aggregation, distinct=distinct)

    queryset = queryset.annotate(
        **formulas
    )

    if order:
        queryset = queryset.order_by(*order)

    if limit:
        queryset = queryset[0:limit]

    # Values to return
    queryset = queryset.values(*groups, *keys, *additional_fields)

    results = []
    async for result in queryset:
        for field in groups:
            if fields.get(field):
                result[field] = fields[field][result[field]]

        results.append(result)

    return results, groups


def get_dates(match, timezone):
    """Resolve a relative-date expression like ``-7d``/``+1m``/``-2y`` into
    a concrete datetime in the given timezone."""
    delta_int = match['delta_int']
    delta_time = match['delta_time']

    remove_tz = False if USE_TZ else True
    if delta_time == 'd':
        func = Dates(tz=timezone, remove_tz=remove_tz).day_delta

    elif delta_time == 'm':
        func = Dates(tz=timezone, remove_tz=remove_tz).month_delta

    elif delta_time == 'y':
        func = Dates(tz=timezone, remove_tz=remove_tz).year_delta

    return func(delta_int)


def get_period(period, timezone):
    """Translate a ``period`` request payload into ``(filter_kwargs,
    start_date, end_date)`` for use in queryset filters. Supports absolute
    dates, named periods (``today``, ``yesterday``...) and relative deltas."""
    if not period:
        return {}, None, None

    field = period.get('field')
    start_delta = period.get('start_delta')
    end_delta = period.get('end_delta')
    start_date = period.get('start_date')
    end_date = period.get('end_date')

    if not field and not (start_date or end_date or start_delta or end_delta):
        return {}, None, None

    date_filter = {}

    if start_date:
        date_filter[f'{field}__gte'] = start_date

    if end_date:
        date_filter[f'{field}__lte'] = end_date

    if start_delta:
        match = DELTA_REGEX.search(start_delta)
        if match:
            (start_date, _) = get_dates(match, timezone)
            date_filter[f'{field}__gte'] = start_date

    if end_delta:
        match = DELTA_REGEX.search(end_delta)
        if match:
            (_, end_date) = get_dates(match, timezone)
            date_filter[f'{field}__lte'] = end_date

    return [date_filter, start_date, end_date]


def normalize_dates(results, timezone, start_date, end_date, group_by, groups):
    """Fill missing buckets in a date-grouped result so the chart axis is
    continuous (e.g. days with zero rows still appear with ``0``)."""
    if not results:
        return results

    start = results[0]
    end = results[-1]

    if not start_date:
        start_date = start['date']

    if not end_date:
        end_date = end['date']

    FREQ = {
        'hour': 'H',
        'day': 'D',
        'month': 'MS',
        'year': 'YS'
    }

    if USE_TZ:
        dates = pd.date_range(
            start_date, end_date, freq=FREQ[group_by.lower()], tz=timezone
        )
    else:
        dates = pd.date_range(
            start_date, end_date, freq=FREQ[group_by.lower()],
        )

    distinct = {}
    for result in results:
        for group in groups:
            if group not in distinct:
                distinct[group] = [result[group]]

            elif result[group] not in distinct[group]:
                distinct[group].append(result[group])

    data = {}
    for i in range(len(dates)):
        date = dates[i]
        data[date] = []

        for group in distinct:

            for item in distinct[group]:
                line = {}
                line['date'] = date
                line['y'] = 0
                line[group] = item


def normalize_groups(
    results, additional_fields, calc, timezone, start_date, end_date, group_by, groups
):
    """Reshape grouped results into the ``{x: {series: value}}`` structure
    the front-end expects for charts. Single-group → ``{x: {formula: value}}``.
    Multi-group → ``{x: {second_group_value: calc_value}}``."""
    if not results:
        return results

    keys = []
    values = []

    tmp = OrderedDict()

    x = groups[0]
    calc_key = calc[0]

    single_group = len(groups) == 1
    if not single_group:
        first_group = groups[1]

    for result in results:
        x_key = result[x]
        if x_key not in tmp:
            tmp[x_key] = {}

        for field in additional_fields:
            tmp[x_key][field] = result[field]

        if single_group:
            keys = calc
            for formula in calc:
                tmp[x_key][formula] = result[formula]

            continue

        # Multi-group aggregations support only a single calc formula
        second = str(result[first_group])
        if second not in tmp[x_key]:
            tmp[x_key][second] = result[calc_key]

        if second not in keys:
            keys.append(second)

    for x, results in tmp.items():
        result = {**results}
        if x is None:
            result['x'] = 'Null'
        elif isinstance(x, str) and x.strip() == '':
            result['x'] = 'Empty'
        else:
            result['x'] = x

        values.append(result)

    return values, keys


async def get_results(timezone, data):
    """Entry point for the metrics endpoint.

    ``data`` is the request body. Resolves the model, applies ``conditions``
    via ``OrmFilter``, applies ``period`` filters, and dispatches to
    ``aggregate`` (no groups) or ``group_by`` (with groups), then post-
    processes via ``normalize_dates``/``normalize_groups`` when applicable.
    """
    model = data['model']
    model = get_model(model)

    conditions = data.get('conditions')
    if conditions:
        queryset = OrmFilter(
            model,
            timezone
        )
        queryset = queryset.filter_by(conditions)

    else:
        queryset = model.objects

    timezone = pytz.timezone(timezone)

    additional_fields = data.get('additional_fields', [])
    order = data.get('order', [])
    limit = data.get('limit')
    raw = data.get('raw')
    keys = data.get('keys')

    distinct = data.get('distinct', False)

    formulas = data.get('calc', {'formula': ['count'], 'field': 'id'})
    calc = formulas.get('formula', ['count'])

    on_field = formulas.get('field', 'id')

    groups = data.get('group_by', {})

    date_group = groups.get('date', {})
    groups = groups.get('fields', [])

    extra = data.get('extra')

    filter_by = data.get('filter_by', {})
    filtered_by_fields = filter_by.get('fields', {})

    filter_by_fields = {}
    or_conditions = Q()

    for key, value in filtered_by_fields.items():
        if key == 'or_conditions':
            for condition in value:
                or_conditions |= Q(**condition)
        else:
            filter_by_fields[key] = value

    queryset = queryset.filter(**filter_by_fields)
    if or_conditions:
        queryset = queryset.filter(or_conditions)

    filter_by_period = filter_by.get('period')
    start_date = end_date = None

    if filter_by_period:
        period_filter, start_date, end_date = get_period(filter_by_period, timezone.zone)
        queryset = queryset.filter(**period_filter)

    if extra:
        queryset = queryset.extra(**extra)

    if groups or date_group:
        results, groups = await group_by(
            queryset, on_field, additional_fields, calc, groups,
            order, timezone, date_group, limit, distinct, model
        )

        results_keys = []
        if results:
            if raw:
                results_keys = groups
            else:
                results, results_keys = normalize_groups(
                    results, additional_fields, calc, timezone, start_date, end_date,
                    date_group.get('group_by'), groups
                )

        if keys:
            new_keys = []
            new_results = []
            for key in results_keys:
                new_keys.append(keys[key])

            for result in results:
                new_result = {**result}
                for key, value in result.items():
                    if key in keys:
                        new_result[keys[key]] = value

                new_results.append(new_result)

            results = new_results
            results_keys = new_keys

        data = {
            'data': results,
            'keys': results_keys
        }

    else:
        data = await aggregate(
            queryset, on_field, calc, timezone, distinct
        )

    return data
