import datetime
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

import jsonschema
import pendulum

from prefect._internal.pydantic import HAS_PYDANTIC_V2
from prefect._internal.schemas.fields import DateTimeTZ
from prefect.exceptions import InvalidNameError
from prefect.utilities.annotations import NotSet
from prefect.utilities.dockerutils import get_prefect_image_name
from prefect.utilities.pydantic import JsonPatch

BANNED_CHARACTERS = ["/", "%", "&", ">", "<"]
LOWERCASE_LETTERS_NUMBERS_AND_DASHES_ONLY_REGEX = "^[a-z0-9-]*$"
LOWERCASE_LETTERS_NUMBERS_AND_UNDERSCORES_REGEX = "^[a-z0-9_]*$"

if TYPE_CHECKING:
    from prefect.blocks.core import Block
    from prefect.events.schemas import DeploymentTrigger
    from prefect.utilities.callables import ParameterSchema

    if HAS_PYDANTIC_V2:
        from pydantic.v1.fields import ModelField
    else:
        from pydantic.fields import ModelField


def raise_on_name_with_banned_characters(name: str) -> None:
    """
    Raise an InvalidNameError if the given name contains any invalid
    characters.
    """
    if any(c in name for c in BANNED_CHARACTERS):
        raise InvalidNameError(
            f"Name {name!r} contains an invalid character. "
            f"Must not contain any of: {BANNED_CHARACTERS}."
        )


def raise_on_name_alphanumeric_dashes_only(value, field_name: str = "value"):
    if not bool(re.match(LOWERCASE_LETTERS_NUMBERS_AND_DASHES_ONLY_REGEX, value)):
        raise ValueError(
            f"{field_name} must only contain lowercase letters, numbers, and dashes."
        )
    return value


def raise_on_name_alphanumeric_underscores_only(value, field_name: str = "value"):
    if not bool(re.match(LOWERCASE_LETTERS_NUMBERS_AND_UNDERSCORES_REGEX, value)):
        raise ValueError(
            f"{field_name} must only contain lowercase letters, numbers, and"
            " underscores."
        )
    return value


def validate_schema(schema: dict):
    """
    Validate that the provided schema is a valid json schema.

    Args:
        schema: The schema to validate.

    Raises:
        ValueError: If the provided schema is not a valid json schema.

    """
    try:
        if schema is not None:
            # Most closely matches the schemas generated by pydantic
            jsonschema.Draft4Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise ValueError(
            "The provided schema is not a valid json schema. Schema error:"
            f" {exc.message}"
        ) from exc


def validate_values_conform_to_schema(
    values: dict, schema: dict, ignore_required: bool = False
):
    """
    Validate that the provided values conform to the provided json schema.

    Args:
        values: The values to validate.
        schema: The schema to validate against.
        ignore_required: Whether to ignore the required fields in the schema. Should be
            used when a partial set of values is acceptable.

    Raises:
        ValueError: If the parameters do not conform to the schema.

    """
    from prefect.utilities.collections import remove_nested_keys

    if ignore_required:
        schema = remove_nested_keys(["required"], schema)

    try:
        if schema is not None and values is not None:
            jsonschema.validate(values, schema)
    except jsonschema.ValidationError as exc:
        if exc.json_path == "$":
            error_message = "Validation failed."
        else:
            error_message = (
                f"Validation failed for field {exc.json_path.replace('$.', '')!r}."
            )
        error_message += f" Failure reason: {exc.message}"
        raise ValueError(error_message) from exc
    except jsonschema.SchemaError as exc:
        raise ValueError(
            "The provided schema is not a valid json schema. Schema error:"
            f" {exc.message}"
        ) from exc


### DEPLOYMENT SCHEMA VALIDATORS ###


def infrastructure_must_have_capabilities(
    value: Union[Dict[str, Any], "Block", None],
) -> Optional["Block"]:
    """
    Ensure that the provided value is an infrastructure block with the required capabilities.
    """

    from prefect.blocks.core import Block

    if isinstance(value, dict):
        if "_block_type_slug" in value:
            # Replace private attribute with public for dispatch
            value["block_type_slug"] = value.pop("_block_type_slug")
        block = Block(**value)
    elif value is None:
        return value
    else:
        block = value

    if "run-infrastructure" not in block.get_block_capabilities():
        raise ValueError(
            "Infrastructure block must have 'run-infrastructure' capabilities."
        )
    return block


def storage_must_have_capabilities(
    value: Union[Dict[str, Any], "Block", None],
) -> Optional["Block"]:
    """
    Ensure that the provided value is a storage block with the required capabilities.
    """
    from prefect.blocks.core import Block

    if isinstance(value, dict):
        block_type = Block.get_block_class_from_key(value.pop("_block_type_slug"))
        block = block_type(**value)
    elif value is None:
        return value
    else:
        block = value

    capabilities = block.get_block_capabilities()
    if "get-directory" not in capabilities:
        raise ValueError("Remote Storage block must have 'get-directory' capabilities.")
    return block


def handle_openapi_schema(value: Optional["ParameterSchema"]) -> "ParameterSchema":
    """
    This method ensures setting a value of `None` is handled gracefully.
    """
    from prefect.utilities.callables import ParameterSchema

    if value is None:
        return ParameterSchema()
    return value


def validate_automation_names(
    field_value: List["DeploymentTrigger"], values: dict
) -> List["DeploymentTrigger"]:
    """
    Ensure that each trigger has a name for its automation if none is provided.
    """
    for i, trigger in enumerate(field_value, start=1):
        if trigger.name is None:
            trigger.name = f"{values['name']}__automation_{i}"

    return field_value


### SCHEDULE SCHEMA VALIDATORS ###


def validate_deprecated_schedule_fields(values: dict, logger: logging.Logger) -> dict:
    """
    Validate and log deprecation warnings for deprecated schedule fields.
    """
    if values.get("schedule") and not values.get("schedules"):
        logger.warning(
            "The field 'schedule' in 'Deployment' has been deprecated. It will not be "
            "available after Sep 2024. Define schedules in the `schedules` list instead."
        )
    elif values.get("is_schedule_active") and not values.get("schedules"):
        logger.warning(
            "The field 'is_schedule_active' in 'Deployment' has been deprecated. It will "
            "not be available after Sep 2024. Use the `active` flag within a schedule in "
            "the `schedules` list instead and the `pause` flag in 'Deployment' to pause "
            "all schedules."
        )
    return values


def reconcile_schedules(cls, values: dict) -> dict:
    """
    Reconcile the `schedule` and `schedules` fields in a deployment.
    """

    from prefect.deployments.schedules import (
        create_minimal_deployment_schedule,
        normalize_to_minimal_deployment_schedules,
    )

    schedule = values.get("schedule", NotSet)
    schedules = values.get("schedules", NotSet)

    if schedules is not NotSet:
        values["schedules"] = normalize_to_minimal_deployment_schedules(schedules)
    elif schedule is not NotSet:
        values["schedule"] = None

        if schedule is None:
            values["schedules"] = []
        else:
            values["schedules"] = [
                create_minimal_deployment_schedule(
                    schedule=schedule, active=values.get("is_schedule_active")
                )
            ]

    for schedule in values.get("schedules", []):
        cls._validate_schedule(schedule.schedule)

    return values


def interval_schedule_must_be_positive(v: datetime.timedelta) -> datetime.timedelta:
    if v.total_seconds() <= 0:
        raise ValueError("The interval must be positive")
    return v


def default_anchor_date(v: DateTimeTZ) -> DateTimeTZ:
    if v is None:
        return pendulum.now("UTC")
    return pendulum.instance(v)


def get_valid_timezones(v: str) -> Tuple[str, ...]:
    # pendulum.tz.timezones is a callable in 3.0 and above
    # https://github.com/PrefectHQ/prefect/issues/11619
    if callable(pendulum.tz.timezones):
        return pendulum.tz.timezones()
    else:
        return pendulum.tz.timezones


def validate_rrule_timezone(v: str) -> str:
    """
    Validate that the provided timezone is a valid IANA timezone.

    Unfortunately this list is slightly different from the list of valid
    timezones in pendulum that we use for cron and interval timezone validation.
    """
    from prefect._internal.pytz import HAS_PYTZ

    if HAS_PYTZ:
        import pytz
    else:
        from prefect._internal import pytz

    if v and v not in pytz.all_timezones_set:
        raise ValueError(f'Invalid timezone: "{v}"')
    elif v is None:
        return "UTC"
    return v


def validate_timezone(v: str, timezones: Tuple[str, ...]) -> str:
    if v and v not in timezones:
        raise ValueError(
            f'Invalid timezone: "{v}" (specify in IANA tzdata format, for example,'
            " America/New_York)"
        )
    return v


def default_timezone(v: str, values: Optional[dict] = {}) -> str:
    timezones = get_valid_timezones(v)

    if v is not None:
        return validate_timezone(v, timezones)

    # anchor schedules
    elif v is None and values and values.get("anchor_date"):
        tz = values["anchor_date"].tz.name
        if tz in timezones:
            return tz
        # sometimes anchor dates have "timezones" that are UTC offsets
        # like "-04:00". This happens when parsing ISO8601 strings.
        # In this case we, the correct inferred localization is "UTC".
        else:
            return "UTC"

    # cron schedules
    return v


def validate_cron_string(v: str) -> str:
    from croniter import croniter

    # croniter allows "random" and "hashed" expressions
    # which we do not support https://github.com/kiorky/croniter
    if not croniter.is_valid(v):
        raise ValueError(f'Invalid cron string: "{v}"')
    elif any(c for c in v.split() if c.casefold() in ["R", "H", "r", "h"]):
        raise ValueError(
            f'Random and Hashed expressions are unsupported, received: "{v}"'
        )
    return v


# approx. 1 years worth of RDATEs + buffer
MAX_RRULE_LENGTH = 6500


def validate_rrule_string(v: str) -> str:
    import dateutil.rrule

    # attempt to parse the rrule string as an rrule object
    # this will error if the string is invalid
    try:
        dateutil.rrule.rrulestr(v, cache=True)
    except ValueError as exc:
        # rrules errors are a mix of cryptic and informative
        # so reraise to be clear that the string was invalid
        raise ValueError(f'Invalid RRule string "{v}": {exc}')
    if len(v) > MAX_RRULE_LENGTH:
        raise ValueError(
            f'Invalid RRule string "{v[:40]}..."\n'
            f"Max length is {MAX_RRULE_LENGTH}, got {len(v)}"
        )
    return v


### AUTOMATION SCHEMA VALIDATORS ###


def validate_trigger_within(
    value: datetime.timedelta, field: "ModelField"
) -> datetime.timedelta:
    minimum = field.field_info.extra["minimum"]
    if value.total_seconds() < minimum:
        raise ValueError("The minimum `within` is 0 seconds")
    return value


### INFRASTRUCTURE SCHEMA VALIDATORS ###


def validate_k8s_job_required_components(cls, value: Dict[str, Any]):
    """
    Validate that a Kubernetes job manifest has all required components.
    """
    from prefect.utilities.pydantic import JsonPatch

    patch = JsonPatch.from_diff(value, cls.base_job_manifest())
    missing_paths = sorted([op["path"] for op in patch if op["op"] == "add"])
    if missing_paths:
        raise ValueError(
            "Job is missing required attributes at the following paths: "
            f"{', '.join(missing_paths)}"
        )
    return value


def validate_k8s_job_compatible_values(cls, value: Dict[str, Any]):
    """
    Validate that the provided job values are compatible with the job type.
    """
    from prefect.utilities.pydantic import JsonPatch

    patch = JsonPatch.from_diff(value, cls.base_job_manifest())
    incompatible = sorted(
        [
            f"{op['path']} must have value {op['value']!r}"
            for op in patch
            if op["op"] == "replace"
        ]
    )
    if incompatible:
        raise ValueError(
            "Job has incompatible values for the following attributes: "
            f"{', '.join(incompatible)}"
        )
    return value


def cast_k8s_job_customizations(
    cls, value: Union[JsonPatch, str, List[Dict[str, Any]]]
):
    if isinstance(value, list):
        return JsonPatch(value)
    elif isinstance(value, str):
        try:
            return JsonPatch(json.loads(value))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Unable to parse customizations as JSON: {value}. Please make sure"
                " that the provided value is a valid JSON string."
            ) from exc
    return value


def set_default_namespace(values: dict) -> dict:
    """
    Set the default namespace for a Kubernetes job if not provided.
    """
    job = values.get("job")

    namespace = values.get("namespace")
    job_namespace = job["metadata"].get("namespace") if job else None

    if not namespace and not job_namespace:
        values["namespace"] = "default"

    return values


def set_default_image(values: dict) -> dict:
    """
    Set the default image for a Kubernetes job if not provided.
    """
    job = values.get("job")
    image = values.get("image")
    job_image = (
        job["spec"]["template"]["spec"]["containers"][0].get("image") if job else None
    )

    if not image and not job_image:
        values["image"] = get_prefect_image_name()

    return values
