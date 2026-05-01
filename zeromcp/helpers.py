import re
import zoneinfo
from functools import reduce

from .exception import HTTPException


LOCAL_HOST = re.compile(r'^(localhost|127.0.0.1):*([0-9]+)?$')

re_id = re.compile(
    r'(.*)\/(?:'
    r'(?P<uuid>\b[0-9a-f]{8}\b-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-\b[0-9a-f]{12}\b)'
    r'|(?P<int_id>\d+))(?:\/)?$'
)

search_regex = re.compile(r'__isnull|__gte|__lte|__lt|__gt|__startswith')


_tz_cache = {}


def get_tz(name):
    tz = _tz_cache.get(name)
    if tz is None:
        tz = zoneinfo.ZoneInfo(name)
        _tz_cache[name] = tz
    return tz


async def method_not_allowed(self, **kwargs):
    raise HTTPException(405, 'Method not allowed')


def make_list(data):
    if not data:
        return []
    if not isinstance(data, list):
        return [data]
    return data


async def save_through_model(instance, m2m_field, related_ids):
    related_ids = make_list(related_ids)
    field = instance._meta.get_field(m2m_field)
    through_model = field.remote_field.through
    source_field = field.m2m_field_name()
    target_field = field.m2m_reverse_field_name()

    objs = [
        through_model(**{source_field: instance, target_field + '_id': rid})
        for rid in related_ids
    ]
    await through_model.objects.abulk_create(objs)


def get_edit_related(args, field):
    result = args['result']
    model = args['model']
    obj = args['obj']

    model = model.split('__')

    if len(model) > 1:
        obj = getattr(obj, model[0], None)
        if obj is None:
            result[model[0]] = None
            return args
        model = '__'.join(model[1:])
        reduce(get_edit_related, [field], {'model': model, 'obj': obj, 'result': result})
    else:
        model = model[0]
        keys = field.split('__')
        if len(keys) == 1:
            obj = getattr(obj, model, None)
            if obj is not None:
                result[field] = getattr(obj, field, None)
                if 'id' not in result:
                    result['id'] = getattr(obj, 'id', None)
            else:
                result[field] = None
        else:
            if keys[0] not in result:
                result[keys[0]] = {}
            sub_obj = getattr(obj, model, None)
            if sub_obj is None:
                result[keys[0]] = None
                return args
            reduce(
                get_edit_related,
                keys[1:],
                {'model': keys[0], 'obj': sub_obj, 'result': result[keys[0]]},
            )
            if not result[keys[0]]:
                result[keys[0]] = None
    return args


def get_related_objects(args, model):
    obj = args[0]
    result = args[1]
    count = args[2]
    parent = args[3]
    related_models = args[4]
    related_fields = args[5]
    model_obj = getattr(obj, model, None)

    if count == 0:
        if model not in related_models:
            related_models[model] = []

        if model_obj:
            result[model] = {}
            for field in related_fields[parent]:
                result[model][field] = getattr(model_obj, field, None)
                related_models[model].append(field)
        else:
            result[model] = None
    else:
        return model_obj, result, count - 1, parent, related_models, related_fields
