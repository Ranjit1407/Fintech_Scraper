import StatusBadge from "./StatusBadge";

const LABELS = {
  robots_txt: "robots.txt",
  robots_txt_redirect: "robots.txt (redirect)",
  page_access: "Page access",
  rate_limits: "Rate limits",
  terms_of_service: "Terms of Service",
  official_api: "Official API",
  content_license: "Content licence",
  personal_data: "Personal data",
};

export default function ComplianceTable({ checks = [], compact = false }) {
  return (
    <table className="table compliance">
      <thead>
        <tr>
          <th>Check</th>
          <th>Status</th>
          <th>Result</th>
        </tr>
      </thead>
      <tbody>
        {checks.map((check) => (
          <tr key={check.name}>
            <td className="nowrap">{LABELS[check.name] || check.name}</td>
            <td>
              <StatusBadge status={check.status} />
            </td>
            <td>
              {check.summary}
              {!compact && check.details && Object.keys(check.details).length > 0 && (
                <details className="details">
                  <summary>Details</summary>
                  <pre>{JSON.stringify(check.details, null, 2)}</pre>
                </details>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
