"""JSON Schema generation utilities for ULog topics with array field grouping."""

import re
from typing import Any, Dict, List, Tuple
import numpy as np

ARRAY_FIELD_REGEX = re.compile(r"^(.+)\[(\d+)\]$")


def parse_grouped_fields(raw_fields: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Tuple[str, bool, Any]]]:
    """Parse ULog raw fields (including array subscripts like 'field[0]') into grouped schema structures.

    Returns:
        Tuple of (grouped_schema_fields, field_access_list)
        where field_access_list contains instructions for extract_sample:
        - (field_name, False, arr) for scalar fields
        - (field_name, True, [arr_0, arr_1, ...]) for array fields
    """
    grouped: Dict[str, Any] = {}
    access_list: List[Tuple[str, bool, Any]] = []

    array_elements: Dict[str, Dict[int, Tuple[Any, Any]]] = {}

    for k, v in raw_fields.items():
        dt = getattr(v, "dtype", v)
        m = ARRAY_FIELD_REGEX.match(k)
        if m:
            base_name, idx = m.group(1), int(m.group(2))
            if base_name not in array_elements:
                array_elements[base_name] = {}
            array_elements[base_name][idx] = (v, dt)
        else:
            grouped[k] = {"is_array": False, "dtype": dt}
            access_list.append((k, False, v))

    # Process array items in order of base_name
    for base_name, elem_dict in array_elements.items():
        sorted_indices = sorted(elem_dict.keys())
        first_dt = elem_dict[sorted_indices[0]][1]
        arr_list = [elem_dict[i][0] for i in sorted_indices]
        grouped[base_name] = {"is_array": True, "dtype": first_dt, "length": len(sorted_indices)}
        access_list.append((base_name, True, arr_list))

    return grouped, access_list


def generate_json_schema(topic_name: str, raw_fields: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a JSON schema dict for a ULog dataset with array field grouping.

    Args:
        topic_name: Name of the topic.
        raw_fields: Dict mapping field names (including 'field[0]') to numpy arrays or dtypes.

    Returns:
        JSON Schema dictionary.
    """
    grouped_fields, _ = parse_grouped_fields(raw_fields)
    properties: Dict[str, Dict[str, Any]] = {}

    for field_name, info in grouped_fields.items():
        dtype = info["dtype"]
        if isinstance(dtype, str):
            dt = np.dtype(dtype)
        else:
            dt = dtype

        if np.issubdtype(dt, np.integer):
            item_type = "integer"
        elif np.issubdtype(dt, np.floating):
            item_type = "number"
        elif np.issubdtype(dt, np.bool_):
            item_type = "boolean"
        else:
            item_type = "string"

        if info["is_array"]:
            properties[field_name] = {
                "type": "array",
                "items": {"type": item_type},
            }
        else:
            properties[field_name] = {"type": item_type}

    return {
        "title": topic_name,
        "type": "object",
        "properties": properties,
    }


LOG_TOPIC_SCHEMA: Dict[str, Any] = {
    "title": "log",
    "type": "object",
    "properties": {
        "timestamp": {"type": "integer"},
        "level": {"type": "string"},
        "log_level": {"type": "integer"},
        "message": {"type": "string"},
    },
}
