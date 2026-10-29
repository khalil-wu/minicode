import { useState } from "react";
import { SelectMenu } from "../components/SelectMenu";
import { useI18n } from "../i18n";

export function HandoffLocalBranchSelection({ message, branches, initialBranch, onSelect }: {
  message: string;
  branches: string[];
  initialBranch: string;
  onSelect: (branch: string) => void;
}) {
  const { t: tr } = useI18n();
  const [selected, setSelected] = useState(initialBranch);
  return <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
    <div>{message}</div>
    <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
      <span>{tr("本地工作区将切换到")}</span>
      <SelectMenu value={selected} ariaLabel={tr("本地检出分支")} onValueChange={branch => { setSelected(branch); onSelect(branch); }}>
        {branches.map(branch => <option key={branch} value={branch}>{branch}</option>)}
      </SelectMenu>
    </div>
  </div>;
}
