"""Explicit synthetic package evidence; never connects to runtime capabilities."""

from market_data_foundation.web.package_catalog import (
    CredentialState,
    HistoryEvidence,
    PackageCatalog,
    PackageHistory,
)
from market_data_foundation.web.package_store import LocalPackageStore
from market_data_foundation.web.packages import PackageAccess, PackageHost


def fixture_host(root, *, initialize=True, scenario="default"):
    catalog = PackageCatalog()
    store = (
        LocalPackageStore.initialize(root, catalog)
        if initialize
        else LocalPackageStore(root, catalog)
    )

    def authorize(request, package_id):
        return PackageAccess(
            metadata_view_permission="denied"
            if scenario == "catalog_denied"
            else "allowed",
            write_eligible=False
            if scenario == "read_only"
            else (None if scenario == "write_unknown" else True),
            write_reasons=["synthetic_fixture_permission"],
        )

    def credentials(environment_id, package_id, record):
        sources = sorted(catalog.credential_sources(package_id))
        return [
            CredentialState(
                source_id=s, credential_state="missing" if s == "BEA" else "configured"
            )
            for s in sources
        ]

    def history(environment_id, package_id, record):
        if scenario == "history_unknown":
            return PackageHistory()
        return PackageHistory(
            completeness="COMPLETE",
            history=[
                HistoryEvidence(
                    definition_ref=c.definition_ref,
                    manual_registered=scenario == "financial_manual"
                    and c.capability_id == "financial_daily",
                    holding="present"
                    if c.capability_id == "ppi"
                    or (
                        scenario == "financial_manual"
                        and c.capability_id == "financial_daily"
                    )
                    else "absent",
                    metadata_view_permission="denied"
                    if scenario == "metadata_denied"
                    else ("unknown" if scenario == "metadata_unknown" else "allowed"),
                    read_permission="denied"
                    if scenario == "read_denied"
                    else ("unknown" if scenario == "read_unknown" else "allowed"),
                    publication_readable=False if scenario == "unpublished" else True,
                )
                for c in catalog.package(package_id).capabilities
            ],
        )

    return PackageHost(
        store,
        "synthetic-d01",
        authorize=authorize,
        credentials=credentials,
        history=history,
        credential_replace_enabled=True,
    )
