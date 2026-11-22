"""Bambu AMS wire fields, conservative normalization and material validation."""
import copy
import re


def integer(value):
    try:
        return int(value) if not isinstance(value, bool) else None
    except (ValueError, TypeError, OverflowError):
        return None


def bitmask(value):
    try:
        return int(str(value), 16) if int(str(value), 16) >= 0 else None
    except (ValueError, TypeError):
        return None


def merge_state(old, delta):
    """Lists of identified AMS/tray objects are deltas; explicit [] clears them."""
    result = copy.deepcopy(old)
    for key, value in delta.items():
        if isinstance(value, dict):
            result[key] = merge_state(result.get(key, {}) if isinstance(result.get(key), dict) else {}, value)
        elif key in ('ams', 'tray') and isinstance(value, list) and value and all(isinstance(x, dict) and 'id' in x for x in value):
            entries = {str(x['id']): x for x in (result.get(key) or []) if isinstance(x, dict) and 'id' in x}
            for item in value:
                identity = str(item['id'])
                entries[identity] = merge_state(entries.get(identity, {}), item)
            result[key] = list(entries.values())
        else:
            result[key] = copy.deepcopy(value)
    if 'ams_exist_bits' in delta:
        existing = bitmask(delta['ams_exist_bits'])
        if existing is not None and isinstance(result.get('ams'), list):
            result['ams'] = [u for u in result['ams'] if isinstance(u, dict)
                             and integer(u.get('id')) is not None
                             and 0 <= integer(u['id']) < 32
                             and existing & (1 << integer(u['id']))]
    return result


def flow_number(value, low, high):
    """Dynamic flow (pressure advance) coefficient reported per tray; None when absent/invalid."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 4) if low <= number <= high else None


def normalize_ams(report):
    ams = report.get('ams', {})
    if not isinstance(ams, dict):
        ams = {}
    active = integer(ams.get('tray_now'))
    active_ams, active_slot = (None, None)
    if active is not None and 0 <= active < 254:
        active_ams, active_slot = divmod(active, 4)
    elif active == 254:
        active_ams, active_slot = 255, 0
    try:
        bits = int(str(ams['tray_exist_bits']), 16)
    except (KeyError, ValueError, TypeError):
        bits = None

    def slot(item, aid, sid, external=False):
        tag = item.get('tag_uid')
        known = isinstance(tag, str) and bool(tag) and bool(re.fullmatch(r'[0-9a-fA-F]+', tag))
        rfid = not known or int(tag, 16) != 0
        color = str(item.get('tray_color', '')).upper()
        if not re.fullmatch(r'[0-9A-F]{8}', color):
            color = ''
        remaining = integer(item.get('remain'))
        present = bool(bits & (1 << (aid * 4 + sid))) if bits is not None and not external and aid < 32 else bool(item.get('tray_type'))
        return {'id': sid, 'label': 'External' if external else str(sid + 1), 'present': present,
                'material': str(item.get('tray_type', '')), 'color': color,
                'remaining': remaining if remaining is not None and 0 <= remaining <= 100 else None,
                'rfid': rfid, 'rfid_known': known, 'active': (aid, sid) == (active_ams, active_slot),
                'filament_id': str(item.get('tray_info_idx', '')), 'setting_id': str(item.get('setting_id', '')),
                'flow_k': flow_number(item.get('k'), 0.0, 10.0), 'flow_n': flow_number(item.get('n'), 0.0, 10.0),
                'cali_idx': integer(item.get('cali_idx'))}
    units = []
    unit_map = {integer(u.get('id')): u for u in ams.get('ams', []) if isinstance(u, dict)} if isinstance(ams.get('ams'), list) else {}
    unit_bits = bitmask(ams.get('ams_exist_bits'))
    ids = range(32) if unit_bits is not None else unit_map
    for aid in ids:
        if aid is None or not 0 <= aid <= 31 or (unit_bits is not None and not unit_bits & (1 << aid)):
            continue
        unit = unit_map.get(aid, {})
        tray_map = {integer(t.get('id')): t for t in unit.get('tray', []) if isinstance(t, dict)} if isinstance(unit.get('tray'), list) else {}
        slots = set(sid for sid in tray_map if sid is not None and 0 <= sid <= 3)
        if bits is not None:
            slots.update(sid for sid in range(4) if bits & (1 << (aid * 4 + sid)))
        trays = [slot(tray_map.get(sid, {}), aid, sid) for sid in sorted(slots)]
        humidity = integer(unit.get('humidity'))
        units.append({'id': aid, 'name': 'AMS ' + str(aid + 1), 'humidity': humidity, 'humidity_raw': integer(unit.get('humidity_raw')), 'humidity_unit': 'level', 'slots': trays})
    external = report.get('vt_tray')
    return {'units': units, 'external': slot(external, 255, 0, True) if isinstance(external, dict) else None,
            'active_ams_id': active_ams, 'active_slot_id': active_slot, 'supports_edit_while_printing': False}


def material_payload(params, seq):
    if not isinstance(params, dict):
        raise ValueError('invalid material parameters')
    aid, sid = params.get('ams_id'), params.get('slot_id')
    if type(aid) is not int or aid not in (*range(32), 255) or type(sid) is not int or sid not in range(4) or (aid == 255 and sid != 0):
        raise ValueError('invalid AMS or slot ID')
    for key in ('filament_id', 'setting_id', 'tray_type'):
        value = params.get(key)
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9 _+./-]{1,100}', value):
            raise ValueError('invalid material profile ' + key)
    color = params.get('tray_color')
    if not isinstance(color, str) or not re.fullmatch(r'[0-9A-Fa-f]{8}', color):
        raise ValueError('invalid material color')
    lo, hi = params.get('nozzle_temp_min'), params.get('nozzle_temp_max')
    if type(lo) is not int or type(hi) is not int or not 100 <= lo <= hi <= 350:
        raise ValueError('invalid nozzle temperature range')
    return {'print': {'command': 'ams_filament_setting', 'sequence_id': seq, 'ams_id': aid, 'slot_id': sid,
                      'tray_id': 254 if aid == 255 else sid, 'tray_info_idx': params['filament_id'],
                      'setting_id': params['setting_id'], 'tray_type': params['tray_type'],
                      'tray_color': color.upper(), 'nozzle_temp_min': lo, 'nozzle_temp_max': hi}}


def find_slot(telemetry, params):
    ams = telemetry.get('ams', {})
    if params['ams_id'] == 255:
        return ams.get('external')
    for unit in ams.get('units', []):
        if unit['id'] == params['ams_id']:
            return next((s for s in unit['slots'] if s['id'] == params['slot_id']), None)
    return None


def readback_matches(report, params):
    """Compare durable fields; some A1 firmware never reports setting_id."""
    if params['ams_id'] == 255:
        tray = report.get('vt_tray')
    else:
        tray = None
        ams = report.get('ams', {})
        if not isinstance(ams, dict):
            return False
        for unit in ams.get('ams', []):
            if isinstance(unit, dict) and integer(unit.get('id')) == params['ams_id']:
                tray = next((t for t in unit.get('tray', []) if isinstance(t, dict) and integer(t.get('id')) == params['slot_id']), None)
    if not isinstance(tray, dict):
        return False
    for wire, requested in (('tray_type', 'tray_type'), ('tray_color', 'tray_color'), ('tray_info_idx', 'filament_id')):
        actual = tray.get(wire)
        if not isinstance(actual, str) or actual.upper() != params[requested].upper():
            return False
    if any(integer(tray.get(k)) != params[k] for k in ('nozzle_temp_min', 'nozzle_temp_max')):
        return False
    return 'setting_id' not in tray or tray['setting_id'] == params['setting_id']
