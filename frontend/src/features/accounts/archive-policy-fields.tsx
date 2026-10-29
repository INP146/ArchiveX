import { ArchivePolicy } from "../../lib/api/accounts";
import "./archive-policy-fields.css";

const options: { key: keyof ArchivePolicy; label: string }[] = [
  { key: "archive_originals", label: "原创帖（含引用帖）" },
  { key: "archive_replies", label: "回复" },
  { key: "archive_reposts", label: "转发帖" }
];

export function ArchivePolicyFields({ policy, disabled, onChange }: {
  policy: ArchivePolicy;
  disabled?: boolean;
  onChange: (key: keyof ArchivePolicy, enabled: boolean) => void;
}) {
  return (
    <fieldset className="x-archive-policy" disabled={disabled}>
      <legend>归档内容</legend>
      <div className="x-archive-policy-options">
        {options.map(({ key, label }) => (
          <label key={key}>
            <input type="checkbox" checked={policy[key]}
              onChange={(event) => onChange(key, event.target.checked)} />
            <span>{label}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
