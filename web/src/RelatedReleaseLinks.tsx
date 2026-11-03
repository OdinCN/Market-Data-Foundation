import type { CpiPoint } from "./cpiTimeSeriesApi";
import type { MacroProfile } from "./macroTimeSeriesProfile";
import { releaseLink } from "./macroReleaseLinks";
import { formatBeijingTime } from "./beijingTime";

const roles: Record<string, string> = {
  actual: "当期公布",
  official_previously_reported_prior: "官方原前值",
  prior_as_reported_in_this_event: "报告前值修订",
  previous_estimate: "同季度前一次估计",
  prior_original: "上季度修订前值",
  prior_revised: "后续修订",
  official_previous: "此前公布值",
};
export function RelatedReleaseLinks({
  links,
  profile,
}: {
  links: CpiPoint["related_release_events"];
  profile: MacroProfile;
}) {
  if (!links?.length) return null;
  return (
    <details className="related-release-links">
      <summary>
        {profile.id === "retail" ? "相关月份发布" : "发布记录"}（{links.length}
        ）
      </summary>
      <ul>
        {links.map((link) => (
          <li key={link.event_id}>
            <a href={releaseLink(profile, link.event_id)}>
              {link.source_version?.precision === "date"
                ? link.source_version.value
                : link.source_version?.precision === "instant"
                  ? formatBeijingTime(link.source_version.value)
                  : "查看发布事件"}
              {link.estimate_kind && ` · ${link.estimate_kind}`}
              {!!link.roles?.length &&
                " · " +
                  link.roles
                    .map((role) => roles[role] ?? "相关材料")
                    .join(" / ")}
            </a>
            {link.association === "explicit_version_proof" && (
              <small> · 当前数值已有版本证明</small>
            )}
          </li>
        ))}
      </ul>
      {links.some((link) => link.association !== "explicit_version_proof") && (
        <p className="small">
          相关材料中仍有未与当前表格数值确认对应版本的记录。
        </p>
      )}
    </details>
  );
}
