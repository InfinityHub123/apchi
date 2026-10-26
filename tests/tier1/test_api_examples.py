"""Every example in the contract is a body the model would actually accept.

An example that has drifted from its schema is worse than no example: a reader copies it,
gets a 422, and learns to distrust the reference. This is also the failure mode #167's
documentation pass actually had -- three of its four errors were counts, and proofreading is
what missed them -- so the examples get an assertion rather than a review.

Two things are checked, and the second is the one that bites. Pydantic merges `model_config`
across the MRO, so a response model that subclasses a request model **inherits its example**
-- and the response usually requires fields the request does not. The example then describes
a body that model would reject.
"""

import json
from pathlib import Path
from typing import Any

import pydantic
import pytest
from pydantic import BaseModel

import app.main  # noqa: F401 -- imported for the side effect of loading every model
from app.sections.catalogs.connectors import validate_properties as catalog_properties
from app.sections.event_listeners.schemas import CURATED_SCHEMAS as LISTENER_SCHEMAS
from app.sections.properties import PropertyProblem
from app.sections.properties import validate as validate_against


def listener_properties(listener_type: str, properties: dict[str, Any]) -> dict[str, str]:
    return validate_against(
        LISTENER_SCHEMAS.get(listener_type), properties, listener_type, "listener"
    )


CONTRACT = Path(__file__).resolve().parents[2] / "openapi.json"


def _models() -> list[type[BaseModel]]:
    """Every Pydantic model the app has loaded, by walking the subclass tree.

    Read from the classes rather than from `openapi.json`, so an example is checked against
    the model that declares it. Going via the contract would mean mapping schema names back
    to classes, which is the step that would quietly stop covering something.
    """
    seen: dict[str, type[BaseModel]] = {}

    def walk(cls: type[BaseModel]) -> None:
        for subclass in cls.__subclasses__():
            key = f"{subclass.__module__}.{subclass.__qualname__}"
            if key in seen:
                continue
            seen[key] = subclass
            walk(subclass)

    walk(BaseModel)
    return [model for model in seen.values() if model.__module__.startswith("app.")]


def _declared_examples(model: type[BaseModel]) -> list[Any]:
    extra = model.model_config.get("json_schema_extra")
    if not isinstance(extra, dict):
        return []
    examples = extra.get("examples")
    return list(examples) if isinstance(examples, list) else []


WITH_EXAMPLES = sorted(
    ((model.__name__, model) for model in _models() if _declared_examples(model)),
    key=lambda pair: pair[0],
)


def test_there_are_examples_to_check() -> None:
    """The test below iterates what it finds, so finding nothing would pass silently."""
    assert len(WITH_EXAMPLES) > 10, [name for name, _ in WITH_EXAMPLES]


@pytest.mark.parametrize(("name", "model"), WITH_EXAMPLES, ids=[n for n, _ in WITH_EXAMPLES])
def test_the_example_is_a_body_the_model_accepts(name: str, model: type[BaseModel]) -> None:
    for index, example in enumerate(_declared_examples(model)):
        try:
            model.model_validate(example)
        except pydantic.ValidationError as invalid:
            raise AssertionError(
                f"{name}'s example {index} is not valid for {name}. If {name} inherits this "
                f"example from a parent model, give it one of its own -- Pydantic merges "
                f"model_config across the MRO, so a response model silently adopts its "
                f"request model's example.\n{invalid}"
            ) from invalid


def _by_name() -> dict[str, type[BaseModel]]:
    """Models indexed by the name they appear under in `openapi.json`.

    An ambiguous name is raised rather than resolved: two models sharing one would make this
    check quietly cover the wrong one.
    """
    index: dict[str, type[BaseModel]] = {}
    for model in _models():
        if model.__name__ in index and index[model.__name__] is not model:
            raise AssertionError(f"two models are named {model.__name__}")
        index[model.__name__] = model
    return index


def _route_examples() -> list[tuple[str, str, type[BaseModel], Any]]:
    """Every named request-body example in the contract, with the model it belongs to.

    A different source from the test above, and it has to be: `openapi_examples` on a route
    is attached to the *operation*, not to the model, so nothing in the class tree mentions
    it. These are the examples a reader is most likely to copy -- they are the ones Swagger
    puts in a dropdown with a human summary beside them.
    """
    contract: dict[str, Any] = json.loads(CONTRACT.read_text())
    index = _by_name()
    found = []
    for path, operations in contract["paths"].items():
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            body = operation.get("requestBody") or {}
            for media in body.get("content", {}).values():
                examples = media.get("examples") or {}
                if not examples:
                    continue
                ref = str(media.get("schema", {}).get("$ref", ""))
                model = index.get(ref.rsplit("/", 1)[-1])
                if model is None:
                    continue
                for name, example in examples.items():
                    found.append((f"{method.upper()} {path}", name, model, example["value"]))
    return found


ROUTE_EXAMPLES = _route_examples()


def test_there_are_route_examples_to_check() -> None:
    assert len(ROUTE_EXAMPLES) >= 6, ROUTE_EXAMPLES


@pytest.mark.parametrize(
    ("operation", "name", "model", "example"),
    ROUTE_EXAMPLES,
    ids=[f"{operation} [{name}]" for operation, name, _, _ in ROUTE_EXAMPLES],
)
def test_the_route_example_is_a_body_the_model_accepts(
    operation: str, name: str, model: type[BaseModel], example: Any
) -> None:
    try:
        model.model_validate(example)
    except pydantic.ValidationError as invalid:
        raise AssertionError(
            f"the {name!r} example on {operation} is not a valid {model.__name__}\n{invalid}"
        ) from invalid


#: The two Sections whose properties are checked against a curated schema, and the function
#: that checks them. Pydantic typing is not that check -- `properties` is a `dict[str, Any]`
#: in every one of these models -- so an example can satisfy the model and still be refused
#: by the handler. Which is what happened: a `kafka` listener example was two required
#: properties short, and every assertion above passed it.
CURATED = (
    ("catalogs", "connector", catalog_properties),
    ("event listeners", "type", listener_properties),
)


def _property_examples() -> list[tuple[str, str, Any, Any]]:
    """Every example that carries a `properties` map, with the kind it belongs to.

    Both sources, because the two disagree about where examples live: model-level ones sit
    in `model_config`, and the named ones a reader actually picks from sit on the operation.
    """
    found: list[tuple[str, str, Any, Any]] = []
    for name, model in WITH_EXAMPLES:
        for example in _declared_examples(model):
            found.append((f"model {name}", name, model, example))
    for operation, name, model, example in ROUTE_EXAMPLES:
        found.append((f"{operation} [{name}]", model.__name__, model, example))
    return [
        (where, schema, model, example)
        for where, schema, model, example in found
        if isinstance(example, dict) and example.get("properties")
    ]


PROPERTY_EXAMPLES = _property_examples()


def test_there_are_property_examples_to_check() -> None:
    assert len(PROPERTY_EXAMPLES) >= 4, PROPERTY_EXAMPLES


@pytest.mark.parametrize(
    ("where", "schema", "model", "example"),
    PROPERTY_EXAMPLES,
    ids=[where for where, _, _, _ in PROPERTY_EXAMPLES],
)
def test_the_example_properties_pass_the_curated_schema(
    where: str, schema: str, model: type[BaseModel], example: Any
) -> None:
    """An example a reader copies has to get past the handler, not just past Pydantic.

    Skipped where the kind cannot be told from the example -- a PATCH body that omits
    `connector` or `type` is a legal partial update, and there is nothing to validate the
    properties against without knowing which schema applies.
    """
    for kind, field, validator in CURATED:
        if field not in model.model_fields:
            continue
        named = example.get(field)
        if named is None:
            pytest.skip(f"a partial {kind} update, so no schema applies")
        try:
            validator(named, example["properties"])
        except PropertyProblem as refused:
            raise AssertionError(
                f"{where}: the properties would be refused for the {named!r} {kind[:-1]}."
                f"\n{list(refused.problems)}"
            ) from refused
        return
    pytest.skip("not a Section with a curated property schema")
