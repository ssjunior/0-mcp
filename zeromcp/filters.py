import logging
from datetime import datetime, timedelta
from functools import reduce

logger = logging.getLogger(__name__)

from django.db.models import Func, Q, IntegerField, CharField, FloatField
from django.db.models.functions import Concat
from django.utils import timezone
from operator import __or__ as OR
from operator import __and__ as AND
from pytz import timezone as pytz_timezone

from .constants import CustomAttributePresentations
from .dates import Dates
from .exception import HTTPException
from .util import make_list, normalize_field


def validate_conditions(conditions, allowed_fields):
    """Validate a Layer-2 condition tree against an allowed-fields whitelist.

    ``conditions`` is the JSON tree consumed by :class:`Filter` (dict with
    ``logical_operator`` + ``rules`` or a flat list of rules). ``allowed_fields``
    is any iterable of field names. Each rule's ``field`` (or its root segment
    before ``__``) must appear in the whitelist; otherwise raises
    ``HTTPException(403)``. Custom-attribute fields and the
    ``generated_creation_date`` annotation are exempt.

    Use this from a segment-creation resource's ``hydrate`` (or equivalent
    write hook) to make the segment row safe before persisting — read paths
    can then trust ``segment.conditions`` without re-validating.
    """
    if not allowed_fields:
        raise HTTPException(403, 'Filtering not allowed')

    allowed = set(allowed_fields)

    def check(node):
        if isinstance(node, dict):
            if 'logical_operator' in node:
                for rule in node.get('rules') or []:
                    check(rule)
                return
            field = node.get('field')
            if not field:
                return
            if field.startswith('custom_attributes__') or field.endswith('generated_creation_date'):
                return
            root = field.split('__')[0]
            if field not in allowed and root not in allowed:
                raise HTTPException(403, f'Filter on field "{field}" is not allowed')
        elif isinstance(node, list):
            for item in node:
                check(item)

    check(conditions)


CHAR = [
    'CharField', 'BinaryField', 'FileField', 'FilePathField', 'IPAddressField',
    'GenericIPAddressField', 'SlugField', 'TextField', 'UUIDField'
]

INT = [
    'AutoField', 'BigAutoField', 'BooleanField', 'DecimalField', 'DurationField', 'FloatField',
    'IntegerField', 'BigIntegerField', 'PositiveIntegerField', 'PositiveSmallIntegerField',
    'SmallIntegerField'
]

DATE = [
    'DateField', 'DateTimeField', 'TimeField'
]

OTHER = [
    'BooleanField', 'NullBooleanField', 'OneToOneField'
]


class FormatDigit(Func):
    function = 'LPAD'
    template = "%(function)s(%(expressions)s, 2, '0')"
    output_field = CharField()


class CastFloat(Func):
    function = 'CAST'
    template = "%(function)s(%(expressions)s as DECIMAL(20,5))"
    output_field = FloatField()


class Filter(object):
    """ORM filter builder.

    Translates a JSON condition tree (rules + logical operators) into a
    Django ``QuerySet``. Supports nested AND/OR groups, custom-attribute
    fields, named periods, relative date deltas, isnull coalescence for
    text vs blank, and reverse relation traversal.

    Typical usage:

        f = Filter(MyModel, tz='America/Sao_Paulo')
        qs = f.filter_by(conditions)
    """

    def __init__(self, model, tz='UTC', **kwargs):
        self.db_model = model
        base_queryset = kwargs.get('base_queryset')
        extra_filters = kwargs.get('extra_filters')

        # Use the provided base_queryset if any; otherwise default to model.objects
        if base_queryset is not None:
            self.original_model = base_queryset.all()
        else:
            self.original_model = model.objects

        if extra_filters:
            self.original_model = self.original_model.filter(**extra_filters)

        # Aggregation cannot work with ordered querysets:
        # https://docs.djangoproject.com/en/1.9/topics/db/aggregation/
        # interaction-with-default-ordering-or-order-by
        self.original_model = self.original_model.order_by()

        self.model = self.original_model.all()
        self._tzinfo = pytz_timezone(tz)
        self.now = timezone.now().replace(tzinfo=self._tzinfo)
        self.tz = tz
        self.date_start = None
        self.date_end = None
        self.annotated_fields = []
        self.annotation_period = []
        self.annotation_select = []
        self.model_fields = model._meta._forward_fields_map
        self.working_model = None

    def reset(self):
        self.model = self.original_model.all()

    def change_timezone(self, tz):
        self.tz = tz
        self._tzinfo = pytz_timezone(tz)

    def filter_by_custom_field(self, field):
        """Apply a filter against a custom-attribute field.

        Custom attributes are stored in a separate ``*_custom_attributes``
        related table keyed by name; this builds the proper ``__name=...``
        lookup so the caller can use them as if they were native columns.
        """
        custom = field.split('custom_attributes__')[-1]
        field = '{}_custom_attributes__value'.format(
            self.model.model._meta.model_name
        )
        self.filter_by('custom_attributes__name={}'.format(custom))

    def filter_by_date(self, date_field, date_start=None, date_end=None, **kwargs):
        """Apply a date-range filter on ``date_field``.

        Either ``period`` (named: ``today``, ``yesterday``, ``last_7_days``,
        ...) or explicit ``date_start``/``date_end`` may be supplied. The
        result is normalized so ``start <= end`` and converted into the
        instance timezone before applying the queryset filter.
        """
        if kwargs.get('period') and hasattr(Dates, kwargs['period']):
            period = getattr(Dates(self.tz), kwargs['period'])
            self.date_start, self.date_end = period()
        else:
            self.date_start = date_start or self.date_start
            self.date_end = date_end or self.date_end

            if self.date_start:
                self.date_start = self.date_start.replace(tzinfo=self._tzinfo)
            if self.date_end:
                self.date_end = self.date_end.replace(tzinfo=self._tzinfo)

        if self.date_start and self.date_end and self.date_start > self.date_end:
            self.date_start, self.date_end = self.date_end, self.date_start

        if type(date_field) is not str:
            return

        if date_field.endswith('generated_creation_date'):
            related_model = ''
            related_date_field = date_field.split('__')
            if len(related_date_field) > 1:
                related_model = '{}__'.format(related_date_field[0])

            if self.date_start:
                self.model = self.model.annotate(
                    period=Concat(
                        '{}period_ym'.format(related_model),
                        '{}period_d'.format(related_model),
                        '{}period_h'.format(related_model),
                        output_field=IntegerField()
                    )
                ).filter(period__gte=self.date_start.strftime('%Y%m%d%H'))
            if self.date_end:
                self.model = self.model.annotate(
                    period=Concat(
                        '{}period_ym'.format(related_model),
                        '{}period_d'.format(related_model),
                        '{}period_h'.format(related_model),
                        output_field=IntegerField()
                    )
                ).filter(period__lte=self.date_end.strftime('%Y%m%d%H'))
            self.annotation_select.append('period')

        else:
            if self.date_start:
                date_start = {'{}__{}'.format(date_field, 'gte'): self.date_start}
                self.model = self.model.filter(**date_start)

            if self.date_end:
                date_end = {'{}__{}'.format(date_field, 'lte'): self.date_end}
                self.model = self.model.filter(**date_end)

    def _resolve_field_type(self, rule):
        """Inspect ``rule['field']`` against the model meta to determine
        the lookup ``_type`` ('abc', '123', 'date') and coerce string
        booleans to native ``bool``. Returns ``(field, _type, value)``.

        Special prefixes (``custom_attributes__`` / suffix
        ``generated_creation_date``) are not introspected: the original
        rule type is preserved.
        """
        field = rule['field']
        value = rule.get('value', 0)
        if value is None:
            value = 0
        _type = rule.get('type', 'abc')

        if field.startswith('custom_attributes__') or field.endswith('generated_creation_date'):
            return field, _type, value

        if '__' in field:
            details = field.split('__')
            related = self.db_model._meta.get_field(details[0])
            field_meta = related.related_model._meta.get_field(details[1])
        else:
            field_meta = self.db_model._meta.get_field(field)

        field_type = field_meta.get_internal_type()
        if field_type in CHAR:
            _type = 'abc'
        if field_type == 'BooleanField' and isinstance(value, str):
            value = value.lower() in ['true', '1']
        if field_type in INT:
            _type = '123'
        elif field_type in DATE:
            _type = 'date'

        return field, _type, value

    def _apply_special_values(self, value, operator, negate):
        """Translate the sentinel values 'Blank' and 'Null' into the
        appropriate ORM operator/value pair. Returns ``(value, operator,
        negate)``.

        - 'Blank' becomes ``exact=''`` (negated when the input was
          ``not_exact``).
        - 'Null' becomes ``isnull=True/False`` depending on the input
          operator (``exact`` → True, anything else → False).
        """
        if value == 'Blank':
            return '', 'exact', operator == 'not_exact'
        if value == 'Null':
            return operator == 'exact', 'isnull', False
        return value, operator, negate

    def get_Q(self, filter_by_conditions, logical_operator=None, apply_dates=True):
        """
        {
            "logical_operator": "AND",
            "rules": [
                {
                    "logical_operator": "OR",
                    "rules": [
                        {
                            "field": "email",
                            "operator": "istartswith",
                            "value": "teste"
                        },
                        {
                            "field": "name",
                            "operator": "contains",
                            "value": "teste"
                        }
                    ]
                },
                {
                    "logical_operator": "AND",
                    "rules": [
                        {
                            "field": "email_status",
                            "operator": "exact",
                            "value": "1"
                        },
                        {
                            "field": "optin",
                            "operator": "exact",
                            "value": "1"
                        }
                    ]
                }
            ]
        }
        """
        filter_list = []

        if 'logical_operator' in filter_by_conditions:
            return self.get_Q(
                filter_by_conditions['rules'],
                filter_by_conditions['logical_operator'],
                apply_dates
            )

        for rule in filter_by_conditions:
            _filter = {}

            if 'logical_operator' in rule:
                if rule.get('rules'):
                    get_Q = self.get_Q(rule['rules'], rule['logical_operator'], apply_dates)
                    if get_Q:
                        filter_list.append(get_Q)
            else:
                if not rule.get('field'):
                    continue

                operator = rule.get('operator', 'in')
                if not operator:
                    continue

                field, _type, value = self._resolve_field_type(rule)

                negate = operator.startswith('not_')
                coalesce = operator in ('isnull', 'isnotnull')

                if operator in ['in', 'not_in'] and not value:
                    continue

                if _type == 'date' and not apply_dates:
                    continue

                value, operator, negate = self._apply_special_values(value, operator, negate)

                if negate:
                    operator = operator.replace('not_', '')

                if field == 'birthdate':
                    now = datetime.now(self._tzinfo)
                    if operator == 'today':
                        filter_list += [
                            Q(**{'{}__month'.format(field): now.month}),
                            Q(**{'{}__day'.format(field): now.day})
                        ]

                    elif operator == 'this_month':
                        filter_list.append(Q(**{'{}__month'.format(field): now.month}))

                    elif 'age' in operator:
                        operator = operator.split('_')[0]
                        if operator == 'range':
                            try:
                                min_type = value.get('min_value').get('type')
                                max_type = value.get('max_value').get('type')
                                min_value = value.get('min_value').get('value')
                                max_value = value.get('max_value').get('value')
                                if not (min_type or max_type or min_value or max_value):
                                    continue
                            except AttributeError:
                                continue
                        else:
                            if not (value.get('type') and value.get('value')):
                                continue

                        d1, d2 = Dates(self.tz).age(value, operator)
                        if operator in ['range', 'exact']:
                            operator = 'range'
                            filter_list += [
                                Q(**{'{}__{}'.format(field, operator): [d1, d2]})
                            ]
                        elif operator == 'gte':
                            filter_list += [
                                Q(**{'{}__{}'.format(field, operator): d1})
                            ]
                        elif operator == 'lte':
                            filter_list += [
                                Q(**{'{}__{}'.format(field, operator): d2})
                            ]
                    else:
                        method = getattr(Dates(self.tz), operator)
                        d1, d2 = method()
                        filter_list += [
                            Q(**{'{}__month__range'.format(field): [d1.month, d2.month]}),
                            Q(**{'{}__day__range'.format(field): [d1.day, d2.day]})
                        ]

                    continue

                elif _type == 'date' and hasattr(Dates, operator) and apply_dates and not coalesce:
                    method = getattr(Dates(self.tz, False), operator)
                    value = method()
                    operator = 'range'

                elif _type == 'date' and 'age' in operator:
                    operator = operator.split('_')[0]
                    time = value.get('time', 'past')

                    if time == 'past':
                        if operator == 'gt':
                            operator = 'lt'
                        elif operator == 'lt':
                            operator = 'gt'

                    else:
                        value['value'] = -int(value['value'])

                    if operator == 'range':
                        try:
                            min_type = value.get('min_value').get('type')
                            max_type = value.get('max_value').get('type')
                            min_value = value.get('min_value').get('value')
                            max_value = value.get('max_value').get('value')
                            if not (min_type or max_type or min_value or max_value):
                                continue
                        except AttributeError:
                            continue
                    else:
                        if not (value.get('type') and value.get('value')):
                            continue

                    value = Dates(self.tz).age(value, operator)

                    if operator == 'exact':
                        operator = 'range'
                    else:
                        if time == 'past':
                            value = value[0]
                        else:
                            value = value[1]

                elif _type == 'date' and apply_dates and not coalesce:
                    operator = operator.split('_')[0]
                    try:
                        value = timezone.now() - timedelta(days=int(value))
                    except ValueError:
                        if len(value) > 10:
                            value = datetime.strptime(value, '%Y-%m-%d')
                        else:
                            value = datetime.strptime(value, '%Y-%m-%d').date()

                            if operator == 'gt':
                                value = value + timedelta(days=(1))
                                operator = 'gte'

                            elif operator == 'lte':
                                value = value + timedelta(days=1)
                                operator = 'lt'

                    except (ValueError, TypeError) as exc:
                        # Date parsing failed for a value that doesn't match
                        # any supported format (relative int, YYYY-MM-DD,
                        # full datetime). Log and proceed with the original
                        # value so the ORM raises a clear error downstream
                        # rather than silently returning unexpected results.
                        logger.warning(
                            'filter date parse failed for field=%s value=%r: %s',
                            field, value, exc,
                        )

                    # Discard time component for "exact" date matches
                    if operator == 'exact':
                        if len(str(value)) > 10:
                            value = value.date()
                        operator = 'startswith'

                if field.startswith('custom_attributes__'):
                    if value is True:
                        value = 'true'
                    elif value is False:
                        value = 'false'
                    custom = field.split('custom_attributes__')[-1]
                    custom_field = '{}_custom_attributes__value'.format(
                        self.working_model.model._meta.model_name
                    )

                    null_list = list(self.working_model.filter(**{
                        'custom_attributes__name': custom,
                        '{}'.format(custom_field): ''
                    }).values_list('pk', flat=True))

                    is_checkbox = self.working_model.filter(
                        custom_attributes__name=custom,
                        custom_attributes__presentation_id=CustomAttributePresentations.CHECKBOX
                    ).exists()

                    if is_checkbox and value == 'false':
                        false_list = list(self.working_model.filter(**{
                            'custom_attributes__name': custom,
                            '{}__{}'.format(custom_field, operator): value
                        }).values_list('pk', flat=True))

                        _filter.update({
                            'pk__in': list(self.working_model.exclude(
                                custom_attributes__name=custom
                            ).values_list('pk', flat=True)) + false_list or [0]
                        })

                    elif operator == 'isnull' and value:
                        if value == "true":
                            _filter.update({
                                'pk__in': list(self.working_model.exclude(
                                    custom_attributes__name=custom
                                ).values_list('pk', flat=True)) + null_list or [0]
                            })
                        else:
                            null_set = set(null_list)
                            _filter.update({
                                'pk__in': [
                                    pk for pk in self.working_model.filter(
                                        custom_attributes__name=custom
                                    ).values_list('pk', flat=True)
                                    if pk not in null_set
                                ] or [0]
                            })

                    else:

                        if _type == 'date':
                            if type(value) is str:
                                value = f'{value}'[:19]

                            elif type(value) is tuple:
                                start_date, end_date = value
                                start_date = f'{start_date}'[:19]
                                end_date = f'{end_date}'[:19]
                                value = (start_date, end_date)

                        elif operator in ['lte', 'gte']:
                            value = CastFloat(value)

                        working_model = self.working_model.filter(**{
                            'custom_attributes__name': custom,
                            '{}__{}'.format(custom_field, operator): value
                        })

                        ids = set(working_model.values_list(
                            'pk', flat=True
                        ))
                        null_list = set(null_list)
                        pks = list(ids.difference(null_list))

                        _filter.update({
                            'pk__in': pks or [0]
                        })

                elif field.endswith('generated_creation_date'):
                    related_model = ''
                    related_date_field = field.split('__')
                    if len(related_date_field) > 1:
                        related_model = '{}__'.format(related_date_field[0])

                    period_key = '{}period'.format(related_model)
                    annotate = {
                        period_key: Concat(
                            '{}period_ym'.format(related_model),
                            FormatDigit('{}period_d'.format(related_model)),
                            FormatDigit('{}period_h'.format(related_model)),
                            output_field=IntegerField(),
                        )
                    }

                    if type(value) is tuple:
                        date_start, date_end = value
                        if date_start or date_end:
                            self.working_model = self.working_model.annotate(**annotate)
                        if date_start:
                            _filter['{}__gte'.format(period_key)] = date_start.strftime('%Y%m%d%H')
                        if date_end:
                            _filter['{}__lte'.format(period_key)] = date_end.strftime('%Y%m%d%H')
                    else:
                        self.working_model = self.working_model.annotate(**annotate)
                        _filter.update({
                            '{}__{}'.format(
                                period_key, operator
                            ): value.strftime('%Y%m%d%H')
                        })

                    self.annotation_select.append(f'{related_model}period')
                    self.annotation_period.append(f'{related_model}period')

                # Decide whether to compare against empty values
                elif operator in ['isnull', 'isnotnull'] and coalesce:
                    boolean = True if operator == 'isnull' else False
                    if _type in ['123', 'date', 'fk']:
                        _filter = Q(**{'{}__isnull'.format(field): boolean})
                    else:
                        _filter = (Q(**{'{}__isnull'.format(field): boolean}) | Q(**{field: ''}))

                    filter_list.append(_filter)
                    continue

                else:
                    _filter['{}__{}'.format(field, operator)] = value

                # If filtering through the reverse contacts relation,
                # apply a status filter excluding ContactStatus.DELETED (3)
                if field.startswith('contacts__') and field != 'contacts__status_id':
                    _filter['contacts__status_id__in'] = [1, 2]

                if negate:
                    filter_list.append(~Q(**_filter))
                else:
                    filter_list.append(Q(**_filter))

        if not filter_list:
            return

        if logical_operator == 'AND':
            conditions = reduce(AND, filter_list)
        else:
            conditions = reduce(OR, filter_list)

        return conditions

    def filter_by(self, filter_by_conditions, queryset=None, apply_dates=True, report=False):
        """Apply ``filter_by_conditions`` (dict tree or comma-separated
        string of ``field=value`` pairs) to the working queryset and
        return it. Supports nested logical groups via ``get_Q``."""
        if not self.working_model:
            self.working_model = self.model.all()
            self.overrided = True

        if queryset:
            self.working_model = queryset

        if not filter_by_conditions:
            return self.working_model.filter(pk=0) if not report else self.working_model

        if type(filter_by_conditions) is dict:
            _filter = self.get_Q(filter_by_conditions, apply_dates=apply_dates)
            if _filter:
                self.working_model = self.working_model.filter(_filter)

        else:
            self.filtered_by = {}
            for _filter in make_list(filter_by_conditions.split(',')):
                condition, value = _filter.split('=')
                self.filtered_by[condition] = value

            self.working_model = self.working_model.filter(**self.filtered_by)

        # Clear period annotations to fix distinct query problem
        self.clean_annotation_period()

        if getattr(self, 'overrided', False):
            self.model = self.working_model
            return self.model

        return self.working_model

    def clean_annotation_period(self):
        for annotation in self.annotation_period:
            self.working_model.query.annotations.pop(annotation, None)
        self.annotation_period = []

    def clean_annotation_select(self):
        if self.annotation_select:
            for annotation in self.annotation_select:
                self.working_model.query.annotation_select.pop(annotation, None)
            self.working_model.query.set_group_by()

    def ordered_by(self, *order):
        self.model = self.model.order_by(*order)

    def limit(self, start=None, end=None):
        self.model = self.model[start:end]

    def fields(self, *field_names):
        self.model = self.model.values(*field_names)

    def distinct(self, field):
        """Return the sorted distinct values of ``field`` from the working
        queryset. Falls back to the custom-attribute table when the field
        is namespaced under ``custom_attributes__``."""
        if field.startswith('custom_attributes__'):
            self.filter_by_custom_field(field)
            field = '{}_custom_attributes__value'.format(
                self.model.model._meta.model_name
            )

        fields = self.model.order_by().values_list(field, flat=True).distinct()
        return sorted([normalize_field(field) for field in fields if field is not None])

    def list(self):
        return self.model.all()

    def list_id(self):
        return self.model.objects.values_list('id', flat=True)

    def total(self):
        return self.model.count()

    def annotate(self, fields, filter_custom=True):
        """Annotate the queryset so the given ``fields`` (including
        ``custom_attributes__*`` references) become selectable/orderable.
        Used before ``order_by``/``values`` calls on custom attributes."""
        for field in make_list(fields):
            if field.startswith('custom_attributes__'):
                if filter_custom:
                    self.filter_by_custom_field(field)

                field = '{}_custom_attributes__value'.format(
                    self.model.model._meta.model_name
                )

            if field not in self.annotated_fields:
                self.annotated_fields.append(field)
