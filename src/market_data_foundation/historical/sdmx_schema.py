"""Bounded metadata grammar for the exact T08 SDMX 2.1 surfaces.

This validates retained XML in place. It does not fetch XSDs, resolve external
concepts/codelists, interpret annotations, or claim general SDMX conformance.
"""

import math
import re
from decimal import Decimal

from market_data_foundation.canonical.model import require


def validate_value(component, value, codelists):
    """Check original lexical content against an already validated local format.

    Referenced-only code lists remain references, as for the bounded dimension
    contract; an inline list, when retained, must contain the actual code.
    This never resolves external concepts or treats an opaque attribute as time.
    """
    from .sdmx_official import NS, _one, _ref

    rep = _one(component, "structure:LocalRepresentation")
    for fmt in (
        *rep.findall("structure:TextFormat", NS),
        *rep.findall("structure:EnumerationFormat", NS),
    ):
        require(
            int(fmt.get("minLength", "0"))
            <= len(value)
            <= int(fmt.get("maxLength", "2000000")),
            "sdmx_value_length_mismatch",
        )
        kind = fmt.get("textType")
        if kind == "BigInteger":
            require(re.fullmatch(r"[+-]?[0-9]+", value), "sdmx_integer_invalid")
        elif kind == "Double":
            require(
                re.fullmatch(
                    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
                    r"(?:[Ee][+-]?[0-9]+)?",
                    value,
                ),
                "sdmx_double_invalid",
            )
            number = float(value)
            require(
                math.isfinite(number) and (number != 0 or Decimal(value).is_zero()),
                "sdmx_double_invalid",
            )
        # String is a lexical carrier; measure positivity/numeric syntax and
        # provider-specific calendar validity are checked by the data parser.
    for enum in rep.findall("structure:Enumeration", NS):
        ref = _ref(enum[0], "Codelist")
        if ref in codelists:
            require(value in codelists[ref], "sdmx_value_outside_enumeration")


def validate_structure(root, surface, role):
    # Late import keeps the shared wire parser independent of transport/storage.
    from .sdmx_official import (
        NS,
        XML_LANG,
        _identity,
        _identity_values,
        _no_text,
        _one,
        _ref,
    )

    def q(name):
        prefix, local = name.split(":")
        return f"{{{NS[prefix]}}}{local}"

    def shape(node, attrs=(), children=(), repeated=(), text=False):
        require(set(node.attrib) <= set(attrs), "sdmx_metadata_attributes")
        allowed = {q(n) for n in children}
        require(all(c.tag in allowed for c in node), "sdmx_metadata_component")
        for tag in allowed - {q(n) for n in repeated}:
            require(
                sum(c.tag == tag for c in node) <= 1,
                "sdmx_missing_or_ambiguous_component",
            )
        require(
            not (node.tail or "").strip() and (text or _no_text(node)),
            "sdmx_metadata_hidden_content",
        )

    def descriptive(node, extra=(), repeated=(), attrs=()):
        common = ("common:Name", "common:Description", "common:Annotations")
        shape(
            node,
            attrs,
            (*common, *extra),
            ("common:Name", "common:Description", *repeated),
        )
        for child in node:
            if child.tag in {q("common:Name"), q("common:Description")}:
                shape(child, (XML_LANG,), text=True)
            elif child.tag == q("common:Annotations"):
                shape(
                    child,
                    children=("common:Annotation",),
                    repeated=("common:Annotation",),
                )
                for annotation in child:
                    shape(
                        annotation,
                        children=(
                            "common:AnnotationTitle",
                            "common:AnnotationType",
                            "common:AnnotationURL",
                            "common:AnnotationText",
                        ),
                        repeated=("common:AnnotationText",),
                    )
                    for leaf in annotation:
                        shape(
                            leaf,
                            (XML_LANG,)
                            if leaf.tag == q("common:AnnotationText")
                            else (),
                            text=True,
                        )

    maintainable = (
        "agencyID",
        "id",
        "version",
        "urn",
        "uri",
        "isFinal",
        "isExternalReference",
    )

    def identity(node):
        result = _identity(node)
        require(result[0] == surface.agency, "sdmx_metadata_agency")
        for field in ("isFinal", "isExternalReference"):
            if field in node.attrib:
                require(
                    node.get(field) in {"true", "false", "0", "1"},
                    "sdmx_metadata_boolean",
                )
        require(
            node.get("isExternalReference", "false") in {"false", "0"},
            "sdmx_external_structure_not_materialized",
        )
        urn = node.get("urn")
        if urn is not None:
            cls = node.tag.rsplit("}", 1)[-1]
            package = "codelist" if cls == "Codelist" else "datastructure"
            agency, identity, version = result
            require(
                urn
                == (
                    f"urn:sdmx:org.sdmx.infomodel.{package}.{cls}="
                    f"{agency}:{identity}({version})"
                ),
                "sdmx_metadata_urn_mismatch",
            )
        return result

    def local_target(branch, targets):
        require(
            not branch.attrib and len(branch) == 1 and _no_text(branch),
            "sdmx_unsupported_reference_structure",
        )
        ref = branch[0]
        require(
            ref.tag == "Ref"
            and set(ref.attrib) == {"id"}
            and len(ref) == 0
            and _no_text(ref)
            and ref.get("id") in targets,
            "sdmx_local_reference_mismatch",
        )
        return ref.get("id")

    def representation(node, component_role):
        shape(
            node,
            children=(
                "structure:Enumeration",
                "structure:EnumerationFormat",
                "structure:TextFormat",
            ),
        )
        enums = node.findall("structure:Enumeration", NS)
        require(
            bool(enums) == (component_role == "Dimension")
            or component_role == "Attribute",
            "sdmx_representation_role_mismatch",
        )
        if enums:
            require(
                len(enums) == 1 and len(node.findall("structure:TextFormat", NS)) == 0,
                "sdmx_unsupported_reference_structure",
            )
            branch = enums[0]
            require(
                not branch.attrib and len(branch) == 1 and _no_text(branch),
                "sdmx_unsupported_reference_structure",
            )
            require(
                _ref(branch[0], "Codelist")[0] == surface.agency, "sdmx_metadata_agency"
            )
        else:
            require(
                component_role != "Dimension"
                and len(node) == 1
                and node[0].tag == q("structure:TextFormat"),
                "sdmx_unsupported_reference_structure",
            )
        for leaf in node:
            if leaf.tag == q("structure:Enumeration"):
                continue
            shape(leaf, ("textType", "minLength", "maxLength", "isSequence"))
            supported = {
                "Dimension": {"String"},
                "TimeDimension": {"ObservationalTimePeriod"},
                "PrimaryMeasure": {"String"}
                if surface.provider == "ECB"
                else {"Double"},
                "Attribute": {"String", "BigInteger"}
                if enums
                else {"String", "BigInteger", "Double"},
            }[component_role]
            require(
                leaf.get("textType") in supported,
                "sdmx_representation_format",
            )
            for field in ("minLength", "maxLength"):
                if field in leaf.attrib:
                    require(
                        re.fullmatch(r"[0-9]{1,7}", leaf.get(field)),
                        "sdmx_representation_format",
                    )
            if "minLength" in leaf.attrib and "maxLength" in leaf.attrib:
                require(
                    int(leaf.get("minLength")) <= int(leaf.get("maxLength")),
                    "sdmx_representation_format",
                )
            if "isSequence" in leaf.attrib:
                require(
                    leaf.get("isSequence") in {"false", "0"},
                    "sdmx_representation_format",
                )

    concept_versions = set()

    def component(node, attrs):
        shape(
            node,
            attrs,
            (
                "structure:ConceptIdentity",
                "structure:LocalRepresentation",
                "structure:AttributeRelationship",
            ),
        )
        concepts = node.findall("structure:ConceptIdentity", NS)
        if concepts:
            branch = concepts[0]
            require(
                not branch.attrib and len(branch) == 1 and _no_text(branch),
                "sdmx_unsupported_reference_structure",
            )
            ref = branch[0]
            require(
                ref.tag == "Ref"
                and set(ref.attrib)
                == {
                    "agencyID",
                    "id",
                    "class",
                    "package",
                    "maintainableParentID",
                    "maintainableParentVersion",
                }
                and len(ref) == 0
                and _no_text(ref),
                "sdmx_unsupported_reference_structure",
            )
            scheme = "ECB_CONCEPTS" if surface.provider == "ECB" else surface.flow
            require(
                ref.get("class") == "Concept"
                and ref.get("package") == "conceptscheme"
                and ref.get("agencyID") == surface.agency
                and ref.get("id") == node.get("id")
                and ref.get("maintainableParentID") == scheme,
                "sdmx_concept_reference_mismatch",
            )
            scheme_ref = _identity_values(
                (ref.get("agencyID"), scheme, ref.get("maintainableParentVersion"))
            )
            concept_versions.add(scheme_ref)
            require(len(concept_versions) == 1, "sdmx_concept_reference_mismatch")
        reps = node.findall("structure:LocalRepresentation", NS)
        require(len(reps) == 1, "sdmx_missing_or_ambiguous_component")
        for rep in reps:
            representation(rep, node.tag.rsplit("}", 1)[-1])

    # Metadata documents carry only their admitted top-level collections.
    shape(root, children=("message:Header", "message:Structures"))
    header = _one(root, "message:Header")
    shape(
        header,
        children=tuple(
            "message:" + n for n in ("ID", "Test", "Prepared", "Sender", "Receiver")
        ),
    )
    for leaf in header:
        if leaf.tag in {q("message:Sender"), q("message:Receiver")}:
            shape(leaf, ("id",), ("common:Name",), ("common:Name",))
            for name in leaf:
                shape(name, (XML_LANG,), text=True)
        else:
            shape(leaf, text=True)
    structures = _one(root, "message:Structures")
    shape(
        structures,
        children={
            "flow": ("structure:Dataflows",),
            "dsd": ("structure:DataStructures", "structure:Codelists"),
            "unit": ("structure:Codelists",),
        }[role],
    )
    for container in structures:
        if container.tag == q("structure:Dataflows"):
            shape(container, children=("structure:Dataflow",))
            flow = _one(container, "structure:Dataflow")
            identity(flow)
            descriptive(flow, ("structure:Structure",), attrs=maintainable)
            # The caller also validates identity and the sole DSD branch.
            branch = _one(flow, "structure:Structure")
            require(
                not branch.attrib and len(branch) == 1 and _no_text(branch),
                "sdmx_unsupported_reference_structure",
            )
            _ref(branch[0], "DataStructure")
        elif container.tag == q("structure:Codelists"):
            shape(
                container,
                children=("structure:Codelist",),
                repeated=("structure:Codelist",),
            )
            for item in container:
                agency, cl_id, version = identity(item)
                descriptive(
                    item, ("structure:Code",), ("structure:Code",), maintainable
                )
                for code in item.findall("structure:Code", NS):
                    descriptive(code, attrs=("id", "urn"))
                    if "urn" in code.attrib:
                        require(
                            code.get("urn")
                            == (
                                "urn:sdmx:org.sdmx.infomodel.codelist.Code="
                                f"{agency}:{cl_id}({version}).{code.get('id')}"
                            ),
                            "sdmx_metadata_urn_mismatch",
                        )
        else:
            shape(container, children=("structure:DataStructure",))
            dsd = _one(container, "structure:DataStructure")
            agency, dsd_id, version = identity(dsd)

            def component_urn(node, cls):
                if "urn" in node.attrib:
                    require(
                        node.get("urn")
                        == (
                            f"urn:sdmx:org.sdmx.infomodel.datastructure.{cls}="
                            f"{agency}:{dsd_id}({version}).{node.get('id')}"
                        ),
                        "sdmx_metadata_urn_mismatch",
                    )

            descriptive(dsd, ("structure:DataStructureComponents",), attrs=maintainable)
            parts = _one(dsd, "structure:DataStructureComponents")
            shape(
                parts,
                children=(
                    "structure:DimensionList",
                    "structure:Group",
                    "structure:AttributeList",
                    "structure:MeasureList",
                ),
                repeated=("structure:Group",),
            )
            dims = _one(parts, "structure:DimensionList")
            component_urn(dims, "DimensionDescriptor")
            shape(
                dims,
                ("id", "urn"),
                ("structure:Dimension", "structure:TimeDimension"),
                ("structure:Dimension",),
            )
            dimensions = dims.findall("structure:Dimension", NS)
            require(
                tuple(n.get("id") for n in dimensions) == surface.dimensions
                and tuple(n.get("position") for n in dimensions)
                == tuple(str(i) for i in range(1, len(surface.dimensions) + 1)),
                "sdmx_dimension_order_mismatch",
            )
            time = _one(dims, "structure:TimeDimension")
            require(
                time.get("id") == "TIME_PERIOD"
                and time.get("position") == str(len(surface.dimensions) + 1),
                "sdmx_time_dimension_mismatch",
            )
            for dim in dims:
                component_urn(dim, dim.tag.rsplit("}", 1)[-1])
                component(dim, ("id", "position", "urn"))
                require(
                    not dim.findall("structure:AttributeRelationship", NS),
                    "sdmx_unsupported_reference_structure",
                )
            measures = _one(parts, "structure:MeasureList")
            component_urn(measures, "MeasureDescriptor")
            shape(measures, ("id", "urn"), ("structure:PrimaryMeasure",))
            measure = _one(measures, "structure:PrimaryMeasure")
            require(measure.get("id") == "OBS_VALUE", "sdmx_measure_mismatch")
            component_urn(
                measure, "Measure" if surface.provider == "ECB" else "PrimaryMeasure"
            )
            component(measure, ("id", "urn"))
            require(
                not measure.findall("structure:AttributeRelationship", NS),
                "sdmx_unsupported_reference_structure",
            )
            groups = set()
            for group in parts.findall("structure:Group", NS):
                component_urn(group, "GroupDimensionDescriptor")
                require(
                    group.get("id") and group.get("id") not in groups,
                    "sdmx_duplicate_group",
                )
                groups.add(group.get("id"))
                shape(
                    group,
                    ("id", "urn"),
                    ("structure:GroupDimension",),
                    ("structure:GroupDimension",),
                )
                targets = []
                for entry in group:
                    shape(entry, children=("structure:DimensionReference",))
                    targets.append(
                        local_target(
                            _one(entry, "structure:DimensionReference"),
                            surface.dimensions,
                        )
                    )
                require(
                    targets and len(set(targets)) == len(targets),
                    "sdmx_local_reference_mismatch",
                )
            for attributes in parts.findall("structure:AttributeList", NS):
                component_urn(attributes, "AttributeDescriptor")
                shape(
                    attributes,
                    ("id", "urn"),
                    ("structure:Attribute",),
                    ("structure:Attribute",),
                )
                seen = set()
                for attribute in attributes:
                    component_urn(attribute, "DataAttribute")
                    require(
                        attribute.get("id") and attribute.get("id") not in seen,
                        "sdmx_duplicate_attribute_definition",
                    )
                    seen.add(attribute.get("id"))
                    component(attribute, ("id", "urn", "assignmentStatus"))
                    require(
                        attribute.get("assignmentStatus")
                        in {"Mandatory", "Conditional"},
                        "sdmx_attribute_assignment",
                    )
                    relationship = _one(attribute, "structure:AttributeRelationship")
                    shape(
                        relationship,
                        children=("structure:Dimension", "structure:PrimaryMeasure"),
                        repeated=("structure:Dimension",),
                    )
                    kinds = {n.tag for n in relationship}
                    require(len(kinds) == 1, "sdmx_unsupported_reference_structure")
                    targets = [
                        local_target(
                            n,
                            surface.dimensions
                            if n.tag == q("structure:Dimension")
                            else {"OBS_VALUE"},
                        )
                        for n in relationship
                    ]
                    require(
                        len(set(targets)) == len(targets),
                        "sdmx_local_reference_mismatch",
                    )
